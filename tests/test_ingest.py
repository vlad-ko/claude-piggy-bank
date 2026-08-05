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
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ingest as ingest_mod  # noqa: E402
from ingest import (  # noqa: E402
    available_projects,
    classify_turn,
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
            by_type, {"unattributed": 1, "task-notification": 1, "system-reminder": 1}
        )

    def test_tool_result_only_user_record_is_not_a_turn(self) -> None:
        # 3 turns total: the tool_result round-trip must not have created one.
        self.assertEqual(
            self.q1("SELECT COUNT(*) n FROM turns")["n"], 3
        )

    def test_api_calls_attributed_to_most_recent_turn(self) -> None:
        prompt_turn = self.q1("SELECT id FROM turns WHERE turn_type = 'unattributed'")
        n = self.q1(
            "SELECT COUNT(*) n FROM api_calls WHERE turn_id = ?", prompt_turn["id"]
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


class ClassifyTurnProvenanceTest(unittest.TestCase):
    """`turn_type` is PROVENANCE -- who submitted the turn -- not topic (#4).

    The old classifier searched topic vocabulary (`cron`, `wakeup`, `PR-cycle`)
    anywhere in the first 600 chars, so it answered "what is this turn ABOUT",
    which is a different question with a different answer. Measured over one
    developer machine's local transcript corpus (48 session files,
    6,643 user turns, checked 2026-08-04) it was wrong in both directions: 117
    of 117 `wakeup` turns merely contained the word mid-prompt (the literal
    never once appeared at offset 0), while 259 genuine `PIPELINE TICK` ticks
    were filed as `human` because the pattern did not list that prefix.

    Every fixture below is synthetic and hand-written; CLAUDE.md forbids
    committing captured session content. The SHAPES are real -- each was
    verified against the corpus -- but the words are invented.
    """

    # -- the filed false positive: topic word, human provenance -------------
    def test_turn_ABOUT_cron_is_not_charged_to_cron(self) -> None:
        text = (
            "this is consuming way too much context (likely because of our "
            "cron addition) - can we look at whether the PR-cycle tick is "
            "worth keeping at all?"
        )
        self.assertEqual(classify_turn(text), "unattributed")

    def test_turn_ABOUT_wakeups_is_not_charged_to_a_wakeup(self) -> None:
        # Shape of the real false positives: a long operator prompt whose body
        # says "THIS WAKEUP:". The word sat at offset >= 40 in 117 of 117
        # corpus matches, never at 0, so provenance was never what was matched.
        text = (
            "Drive the open reviews and keep the queue moving forward. "
            "THIS WAKEUP: (1) re-check the build, (2) report back."
        )
        self.assertEqual(classify_turn(text), "unattributed")
        self.assertNotEqual(classify_turn(text), "wakeup")

    def test_turn_QUOTING_a_marker_is_not_stolen_by_it(self) -> None:
        # A marker deeper in the text is quoted content, not provenance.
        # Anchoring is what stops a human turn with an appended
        # <system-reminder> from being filed as harness-injected.
        text = (
            "why does this turn classify the way it does?\n"
            "<system-reminder>the reminder body</system-reminder>"
        )
        self.assertEqual(classify_turn(text), "unattributed")

    def test_typed_slash_text_without_harness_tags_is_not_a_command(self) -> None:
        # The harness REWRITES a real invocation into <command-*> tags, so bare
        # "/wizard:" prose in the transcript was NOT submitted by the command
        # machinery. Who typed it is a separate question the classifier does
        # not answer, so it refuses rather than crediting a person.
        self.assertEqual(
            classify_turn("/wizard: resume the queue please"), "unattributed"
        )

    # -- the filed false negative: real ticks that fell through -------------
    def test_genuine_pr_cycle_tick_is_cron_tick(self) -> None:
        text = (
            "PR-CYCLE TICK. Drive every open PR to merge-ready, then report "
            "the queue depth."
        )
        self.assertEqual(classify_turn(text), "cron-tick")

    def test_genuine_pipeline_tick_is_cron_tick(self) -> None:
        # 259 of these were classified `human` before this fix -- a bucket
        # larger than the entire `cron-tick` bucket it was missing from (145).
        text = "PIPELINE TICK — drive open reviews and keep the queue moving."
        self.assertEqual(classify_turn(text), "cron-tick")

    def test_other_measured_tick_sentinels_are_cron_tick(self) -> None:
        # Two more families measured in the corpus (18 and 2 turns). Which
        # names are TRUSTED, and what happens to one that is not, is
        # CronTickSentinelTest's subject.
        self.assertEqual(
            classify_turn("PRECEDENT-GARAGE COHORT TICK — act, do not report."),
            "cron-tick",
        )
        self.assertEqual(
            classify_turn("RESOURCE-MANAGER TICK. Measure host capacity."),
            "cron-tick",
        )

    def test_tick_sentinel_must_be_a_whole_word_at_the_start(self) -> None:
        # Guards the sentinel against the obvious over-matches. A human
        # SHOUTING about a ticket is not a scheduler.
        self.assertEqual(classify_turn("TICKET #12 IS STILL OPEN."), "unattributed")
        self.assertEqual(
            classify_turn("please check every TICK. of the pipeline"), "unattributed"
        )
        # TICK must be its OWN word: without the word boundary this shouted
        # sentence matches on the tail of "STICK" and becomes a scheduler.
        self.assertEqual(classify_turn("MAKE IT STICK."), "unattributed")

    def test_turn_QUOTING_a_tick_sentinel_is_not_the_tick(self) -> None:
        # The anchor is what separates this from the filed defect: discussing
        # a tick must not be charged to the tick. This is the exact shape that
        # made issue #11's attribution detector unsafe to build on.
        text = (
            "the scheduler keeps firing 'PIPELINE TICK — drive open reviews' "
            "every ten minutes and it is burning context. can we stop it?"
        )
        self.assertEqual(classify_turn(text), "unattributed")

    # -- the slash-command arm -----------------------------------------------
    def test_slash_command_with_no_stdout_is_local_command(self) -> None:
        # The filed defect: a command that emits no stdout writes only
        # <command-message>/<command-name>, so `"<local-command" in head` missed
        # it and the topic search then filed /wizard as a cron tick.
        text = (
            "<command-message>wizard</command-message>\n"
            "<command-name>/wizard</command-name>\n"
            "<command-args>resume the PR-cycle queue</command-args>"
        )
        self.assertEqual(classify_turn(text), "local-command")

    def test_slash_command_written_name_first_is_local_command(self) -> None:
        # 29 of the 340 corpus command turns lead with <command-name>; both
        # orders occur and both are the harness.
        text = (
            "<command-name>/self-learning</command-name>\n"
            "<command-message>self-learning</command-message>"
        )
        self.assertEqual(classify_turn(text), "local-command")

    def test_local_command_stdout_and_caveat_still_classify(self) -> None:
        self.assertEqual(
            classify_turn("<local-command-stdout>ok</local-command-stdout>"),
            "local-command",
        )
        self.assertEqual(
            classify_turn("<local-command-caveat>Caveat: ...</local-command-caveat>"),
            "local-command",
        )

    # -- the tag arms, now anchored ------------------------------------------
    def test_task_notification_and_system_reminder_anchor_at_start(self) -> None:
        self.assertEqual(
            classify_turn("<task-notification><task-id>t-1</task-id>"),
            "task-notification",
        )
        self.assertEqual(
            classify_turn("<system-reminder>ctx</system-reminder>"), "system-reminder"
        )

    def test_leading_whitespace_does_not_defeat_the_anchor(self) -> None:
        self.assertEqual(
            classify_turn("\n  <task-notification><task-id>t-1</task-id>"),
            "task-notification",
        )
        self.assertEqual(
            classify_turn("\nPR-CYCLE TICK. Drive every open PR."), "cron-tick"
        )

    def test_empty_text_is_not_a_provenance_claim(self) -> None:
        self.assertEqual(classify_turn(""), "unattributed")


class ClassifyTurnResidualTest(unittest.TestCase):
    """The residual REFUSES to name a submitter instead of asserting `human`.

    #26 classified by provenance but kept `human` as the only exit for an
    unrecognised opening, and `index.html` renders it as a peer row beside
    `task-notification`. That makes "no marker found" read as the positive
    claim "a person typed this" -- absence rendered as a value, the rule
    CLAUDE.md leads with.

    Re-measured over one developer machine's local transcript corpus (49
    main-thread session files, 7,199 user turns, checked 2026-08-05), the
    3,877-turn `human` bucket #26 published held at least 2,143 turns that
    provably were not a person typing. The four largest were structural and
    are recognised below; each sat at offset 0 in 100% of its occurrences:
    the compaction literal (105/105), the skill-injection preamble (342/342),
    the interrupt notice (35/35) and the image placeholder (33/33).

    Every fixture is synthetic and hand-written; no captured session content,
    and no path that names a user.
    """

    def test_unrecognised_opening_refuses_to_claim_a_human_typed_it(self) -> None:
        # The load-bearing assertion of this class. "No marker matched" is not
        # evidence of a person, and must not be reported as one.
        self.assertEqual(classify_turn("please rebase this branch"), "unattributed")
        self.assertNotEqual(classify_turn("please rebase this branch"), "human")

    def test_compaction_continuation_is_claude_code_not_a_person(self) -> None:
        # A FIXED Claude Code literal at offset 0 -- structurally stronger
        # evidence than the tick sentinel the classifier already trusts, yet
        # #26 filed all 105 corpus occurrences as `human`. These turns are
        # large (they carry the whole rolled-up summary), so booking them to a
        # person misattributes real context spend, not just a label.
        text = (
            "This session is being continued from a previous conversation that "
            "ran out of context. The conversation is summarized below:\n"
            "Analysis: the operator asked for a rebase."
        )
        self.assertEqual(classify_turn(text), "compaction")

    def test_skill_injection_preamble_is_the_harness(self) -> None:
        # 342 corpus turns, the single largest structural family inside the old
        # `human` bucket. The harness prepends this when a skill is loaded.
        text = (
            "Base directory for this skill: /tmp/skills/example-skill\n"
            "Use this to resolve relative paths."
        )
        self.assertEqual(classify_turn(text), "skill-injection")

    def test_interrupt_and_image_placeholders_are_harness_notices(self) -> None:
        # Both are text the CLI SUBSTITUTES for something the user did not type
        # as prose. One label, because the emitter is the same.
        self.assertEqual(
            classify_turn("[Request interrupted by user]"), "harness-notice"
        )
        self.assertEqual(
            classify_turn("[Request interrupted by user for tool use]"),
            "harness-notice",
        )
        self.assertEqual(
            classify_turn("[Image: source: /tmp/screenshot.png]"), "harness-notice"
        )

    def test_bash_mode_tags_are_local_commands(self) -> None:
        # Same class of gap as the `<command-message>` miss #26 fixed: these are
        # Claude Code's own tags and were falling into the residual. A full
        # inventory of leading `<tag>` forms in the corpus is now covered --
        # these two were the only ones left unhandled (12 turns each).
        self.assertEqual(
            classify_turn("<bash-input>gh auth status</bash-input>"), "local-command"
        )
        self.assertEqual(
            classify_turn("<bash-stdout>ok</bash-stdout>"), "local-command"
        )

    def test_a_quoted_marker_deeper_in_the_text_is_not_provenance(self) -> None:
        # The anchor has to hold for the new markers too, or a person asking
        # about compaction becomes compaction.
        text = (
            "why do I keep seeing 'This session is being continued from a "
            "previous conversation that ran out of context'? it is expensive."
        )
        self.assertEqual(classify_turn(text), "unattributed")

    def test_scheduler_prefixes_stay_in_the_residual_unguessed(self) -> None:
        # 1,573 `[auto-resume ...]` + 106 `autonomous ...` + 28
        # `[wizard heartbeat]` turns. Plainly bulk-injected, but no component of
        # Claude Code writes them -- they are one scheduling tool's prose
        # convention, and a person can type the same characters. Naming an
        # emitter here would be the guess #26 set out to remove. Now that the
        # residual refuses instead of asserting, leaving them there costs
        # nothing: `unattributed` is the true statement about them.
        self.assertEqual(
            classify_turn("[auto-resume pipeline] sweep the open reviews"),
            "unattributed",
        )
        self.assertEqual(
            classify_turn("autonomous - drive the queue to merge-ready"),
            "unattributed",
        )


class CronTickSentinelTest(unittest.TestCase):
    """A tick is a LISTED sentinel; a tick-SHAPED opener is flagged, not guessed.

    #26 matched a shape (`[A-Z][A-Z0-9 +/&-]{0,40}\\bTICK\\b\\s*[.:-]`) and
    asserted `cron-tick` on it. A shape cannot do that job, because it is asked
    to pull in two opposite directions at once: reject a person shouting about
    the tick, and accept a scheduler name nobody has listed yet. It failed
    both -- `STOP THE TICK. it is eating my context` was booked as the tick's
    own cost (the exact defect #26 says it fixed, now requiring caps lock),
    while a sentinel closed by a NEWLINE fell silently into the residual.

    So: an enumerated list ASSERTS, and the shape only routes the leftovers to
    a loud `cron-tick-unlisted` bucket that surfaces drift instead of guessing
    in either direction. The list is the four families measured on the
    reference corpus; sentinel names are invented here, as fixtures must be.
    """

    # -- direction 1: a person shouting is not the scheduler -----------------
    def test_a_person_shouting_about_the_tick_is_never_the_tick(self) -> None:
        # Reproduced verbatim against #26's implementation: all three returned
        # `cron-tick`, i.e. a person complaining about the tick was charged to
        # the tick. They are tick-SHAPED, so refusing (`cron-tick-unlisted`) is
        # the honest answer -- but they must never be asserted as the tick.
        for text in (
            "STOP THE TICK. it is eating my context",
            "WHY IS THE TICK: firing so often?",
            "TODO FIX THE TICK.",
        ):
            with self.subTest(text=text):
                self.assertNotEqual(classify_turn(text), "cron-tick")
                self.assertEqual(classify_turn(text), "cron-tick-unlisted")

    # -- direction 2: a listed sentinel must not need punctuation ------------
    def test_a_listed_sentinel_closed_by_a_newline_is_still_the_tick(self) -> None:
        # #26 required a closing `[.:-]`, so a scheduler that terminates its
        # sentinel with a newline -- or with nothing -- failed SILENTLY into the
        # residual. Silent failure is what the shape was chosen to avoid.
        self.assertEqual(classify_turn("PIPELINE TICK\nDo the thing"), "cron-tick")
        self.assertEqual(classify_turn("PIPELINE TICK"), "cron-tick")

    def test_listed_sentinels_with_punctuation_still_classify(self) -> None:
        self.assertEqual(
            classify_turn("PR-CYCLE TICK. Drive every open PR."), "cron-tick"
        )
        self.assertEqual(
            classify_turn("PIPELINE TICK - drive open reviews."), "cron-tick"
        )
        self.assertEqual(
            classify_turn("RESOURCE-MANAGER TICK. Measure host capacity."),
            "cron-tick",
        )

    def test_a_listed_sentinel_must_be_a_whole_token(self) -> None:
        # `PIPELINE TICKET` is not `PIPELINE TICK`.
        self.assertEqual(classify_turn("PIPELINE TICKET #12 IS OPEN."), "unattributed")

    # -- the drift bucket ----------------------------------------------------
    def test_an_unlisted_tick_name_is_flagged_rather_than_guessed(self) -> None:
        # The reason the list is safe: a scheduler name nobody has added shows
        # up as its own operator-visible bucket carrying its own token cost,
        # instead of being silently absorbed by either neighbour.
        self.assertEqual(
            classify_turn("DEPLOY-WATCH TICK - redeploy if main is green"),
            "cron-tick-unlisted",
        )
        self.assertEqual(classify_turn("DEPLOY-WATCH TICK\ngo"), "cron-tick-unlisted")

    def test_tick_must_END_the_sentinel_to_be_tick_shaped(self) -> None:
        # A shouted sentence that merely uses the word mid-phrase is not a
        # sentinel in any direction, and must not pollute the drift bucket.
        self.assertEqual(
            classify_turn("ALL TICK BOXES MUST BE CHECKED"), "unattributed"
        )

    def test_the_word_boundary_still_holds_in_both_buckets(self) -> None:
        self.assertEqual(classify_turn("MAKE IT STICK."), "unattributed")
        self.assertEqual(classify_turn("TICKET #12 IS STILL OPEN."), "unattributed")
        self.assertEqual(
            classify_turn("please check every TICK. of the pipeline"), "unattributed"
        )

    def test_quoting_a_listed_sentinel_is_not_speaking_it(self) -> None:
        text = (
            "the scheduler keeps firing 'PIPELINE TICK - drive open reviews' "
            "every ten minutes and it is burning context. can we stop it?"
        )
        self.assertEqual(classify_turn(text), "unattributed")


class ScheduledWakeupProvenanceTest(unittest.TestCase):
    """What actually happened to the turns the pre-#26 code called `wakeup`.

    `classify_turn`'s docstring justified dropping the `wakeup` label with "a
    scheduled wakeup announces itself with a tick sentinel and is classified
    `cron-tick`". That is false. Replaying the 117 turns the old code labelled
    `wakeup` through #26's classifier yields `{'human': 114, 'local-command':
    3}` -- ZERO reach `cron-tick` (checked 2026-08-05). Their real openings are
    scheduler prose (`[auto-resume ...]`, `autonomous - ...`) and, three times,
    Claude Code's compaction literal.

    Dropping the label is still right -- 117 of 117 were topic matches on a
    word sitting at offset >= 40 -- but the reason has to be true, because 114
    of those turns landed in `human` and were then read as people typing.
    """

    def test_a_scheduled_wakeup_carries_no_tick_sentinel(self) -> None:
        for text in (
            "[auto-resume pipeline] FIRM-SCOPE sweep. re-check the build.",
            "autonomous - drive the cohort to merge-ready.",
        ):
            with self.subTest(text=text):
                self.assertNotEqual(classify_turn(text), "cron-tick")
                self.assertNotEqual(classify_turn(text), "cron-tick-unlisted")
                # ... and, the part that was actually wrong: they are not
                # evidence of a person either.
                self.assertEqual(classify_turn(text), "unattributed")

    def test_a_wakeup_era_turn_may_be_a_compaction_continuation(self) -> None:
        # 3 of the 117 were this. They are Claude Code's own text.
        text = (
            "This session is being continued from a previous conversation that "
            "ran out of context. Wakeup cadence was the topic."
        )
        self.assertEqual(classify_turn(text), "compaction")

    def test_the_topic_word_wakeup_never_decides_provenance(self) -> None:
        text = (
            "Drive the open reviews and keep the queue moving forward. "
            "THIS WAKEUP: (1) re-check the build, (2) report back."
        )
        self.assertEqual(classify_turn(text), "unattributed")
        self.assertNotEqual(classify_turn(text), "wakeup")


class ClassifyTurnEndToEndTest(unittest.TestCase):
    """The reclassification survives the write path into `turns.turn_type`.

    Asserts the STATE CHANGE, not that classify_turn returned a string: the
    consumer (issue #11's wakeup/heartbeat attribution) reads the column, not
    the function. Fixture is built inline and is entirely synthetic.
    """

    def _record(self, uuid: str, text: str, ts: str) -> str:
        return json.dumps(
            {
                "type": "user",
                "uuid": uuid,
                "sessionId": "sess-classify",
                "timestamp": ts,
                "message": {"role": "user", "content": text},
            }
        )

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cpb-classify-test-"))
        projects = self.tmp / "projects"
        projects.mkdir(parents=True)
        lines = [
            self._record(
                "u1",
                "<command-message>wizard</command-message>\n"
                "<command-name>/wizard</command-name>",
                "2026-08-04T10:00:00.000Z",
            ),
            self._record(
                "u2",
                "PR-CYCLE TICK. Drive every open PR to merge-ready.",
                "2026-08-04T10:01:00.000Z",
            ),
            self._record(
                "u3",
                "PIPELINE TICK — drive open reviews and keep the queue moving.",
                "2026-08-04T10:02:00.000Z",
            ),
            self._record(
                "u4",
                "can we drop the cron tick? it eats context on every wakeup.",
                "2026-08-04T10:03:00.000Z",
            ),
            self._record(
                "u5",
                "This session is being continued from a previous conversation "
                "that ran out of context. The conversation is summarized below:",
                "2026-08-04T10:04:00.000Z",
            ),
            self._record(
                "u6",
                "DEPLOY-WATCH TICK\nredeploy if main is green",
                "2026-08-04T10:05:00.000Z",
            ),
        ]
        (projects / "sess-classify.jsonl").write_text(
            "\n".join(lines) + "\n", encoding="utf-8"
        )
        self.db = self.tmp / "usage.db"
        self.summary = ingest(projects, self.db)
        self.conn = sqlite3.connect(self.db)
        self.conn.row_factory = sqlite3.Row

    def tearDown(self) -> None:
        self.conn.close()
        shutil.rmtree(self.tmp)

    def test_turn_types_written_to_the_db(self) -> None:
        rows = self.conn.execute(
            "SELECT turn_type, COUNT(*) n FROM turns GROUP BY turn_type"
        ).fetchall()
        self.assertEqual(
            {r["turn_type"]: r["n"] for r in rows},
            {
                "local-command": 1,
                "cron-tick": 2,
                "compaction": 1,
                "cron-tick-unlisted": 1,
                "unattributed": 1,
            },
        )

    def test_no_record_was_dropped_as_unparsed(self) -> None:
        self.assertEqual(self.summary["unparsed_records"], 0)
class IngestRunTimestampTest(unittest.TestCase):
    """WHEN ingest last ran is a fact the database did not record at all (#20).

    Nothing in the v6 schema dated a RUN. `ingest_state.mtime` looks like the
    same fact and is not: it is the SOURCE FILE's mtime, so a database
    untouched for a week, holding transcripts that were themselves last
    written a week ago, is indistinguishable from one refreshed a second ago.
    The report then renders week-old totals as current ones -- absence
    rendering as a value, turned on this tool.

    The fixtures pin the run time and the file mtime DELIBERATELY UNEQUAL, so
    an implementation that reports `MAX(ingest_state.mtime)` -- the nearest
    wrong answer, and the one that already exists in the schema -- cannot pass.
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cpb-runstamp-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.projects = self.tmp / "projects"
        self.projects.mkdir()
        self.transcript = self.projects / "session-fixture.jsonl"
        shutil.copy(FIXTURE, self.transcript)
        self.db = self.tmp / "usage.db"

    def _recorded_runs(self) -> list[float]:
        conn = sqlite3.connect(self.db)
        try:
            return [
                r[0]
                for r in conn.execute(
                    f"SELECT finished_at FROM {ingest_mod.INGEST_RUNS_TABLE}"
                )
            ]
        finally:
            conn.close()

    def test_a_completed_run_records_when_it_finished(self) -> None:
        before = time.time()
        ingest(self.projects, self.db)
        after = time.time()
        runs = self._recorded_runs()
        self.assertEqual(len(runs), 1, "the run stamp is one row, not a log")
        self.assertGreaterEqual(runs[0], before)
        self.assertLessEqual(runs[0], after)

    def test_the_run_time_is_not_the_source_file_mtime(self) -> None:
        # Pinned a day back: a run stamp derived from the transcript's own
        # mtime -- the fact already in `ingest_state` -- lands here instead.
        mtime = time.time() - 86400.0
        os.utime(self.transcript, (mtime, mtime))
        ingest(self.projects, self.db)
        (recorded,) = self._recorded_runs()
        self.assertGreater(
            recorded,
            mtime + 3600.0,
            "the run stamp is when INGEST ran, never the source file's mtime",
        )

    def test_a_run_that_ingests_nothing_still_counts_as_a_run(self) -> None:
        # The load-bearing case from #20: a fresh ingest that found nothing new
        # is HEALTHY, and must not read like no ingest at all. If the stamp
        # were written per ingested file, an all-skipped run would leave it
        # ageing forever and the page would cry stale over current data.
        ingest(self.projects, self.db)
        (first,) = self._recorded_runs()
        time.sleep(0.01)
        summary = ingest(self.projects, self.db)
        self.assertEqual(summary["files_ingested"], 0)
        self.assertGreater(summary["files_skipped"], 0)
        (second,) = self._recorded_runs()
        self.assertGreater(second, first)


class AdditiveSchemaUpgradeTest(unittest.TestCase):
    """Adding a table must not cost the rows a re-ingest cannot reproduce (#20).

    `_prepare_schema` drops and rebuilds on a version change, and REFUSES to
    do so when any tracked source is gone from disk (`SchemaRebuildSafetyTest`
    above). Both are right for a shape change. Neither is right for v6 -> v7,
    which only ADDS a table: a rebuild would discard every row to re-derive
    identical ones, and on a corpus with a reaped transcript the guard would
    refuse outright -- making a purely additive change unlandable for exactly
    the users whose data this database is the only copy of.

    `test_rebuild_still_proceeds_when_every_source_is_re_readable` above is the
    other side of this boundary: an older shape still takes the rebuild path.
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cpb-upgrade-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.projects = self.tmp / "projects"
        self.projects.mkdir()
        self.transcript = self.projects / "session-fixture.jsonl"
        shutil.copy(FIXTURE, self.transcript)
        self.db = self.tmp / "usage.db"
        ingest(self.projects, self.db)

    def _downgrade_to_v6(self) -> None:
        """Put the database back in the shape v6 shipped: no run stamp at all."""
        conn = sqlite3.connect(self.db)
        try:
            conn.execute(f"DROP TABLE {ingest_mod.INGEST_RUNS_TABLE}")
            conn.execute("PRAGMA user_version = 6")
            conn.commit()
        finally:
            conn.close()

    def _counts(self) -> tuple[int, int]:
        conn = sqlite3.connect(self.db)
        try:
            return (
                conn.execute("SELECT COUNT(*) FROM api_calls").fetchone()[0],
                conn.execute("SELECT COUNT(*) FROM ingest_state").fetchone()[0],
            )
        finally:
            conn.close()

    def test_a_v6_database_upgrades_in_place_without_re_parsing(self) -> None:
        before = self._counts()
        self._downgrade_to_v6()
        summary = ingest(self.projects, self.db)
        self.assertFalse(summary["schema_rebuilt"])
        # A rebuild empties `ingest_state`, so every file would be re-parsed.
        # Skipping them all is the observable proof the rows survived.
        self.assertEqual(summary["files_ingested"], 0)
        self.assertEqual(summary["files_skipped"], summary["files_scanned"])
        self.assertEqual(self._counts(), before)
        conn = sqlite3.connect(self.db)
        try:
            self.assertEqual(
                conn.execute("PRAGMA user_version").fetchone()[0],
                ingest_mod.SCHEMA_VERSION,
            )
            self.assertEqual(
                conn.execute(
                    f"SELECT COUNT(*) FROM {ingest_mod.INGEST_RUNS_TABLE}"
                ).fetchone()[0],
                1,
            )
        finally:
            conn.close()

    def test_the_upgrade_is_not_refused_over_a_reaped_source(self) -> None:
        self.transcript.unlink()
        ingest(self.projects, self.db)  # archives it; rows retained
        before = self._counts()
        self._downgrade_to_v6()
        ingest(self.projects, self.db)  # must not raise SystemExit
        self.assertEqual(self._counts(), before)
        conn = sqlite3.connect(self.db)
        try:
            archived = conn.execute(
                "SELECT COUNT(*) FROM ingest_state WHERE archived_at IS NOT NULL"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(archived, 1, "the archived source survived the upgrade")


# An appended API call whose every token class is DELIBERATELY UNEQUAL to
# every other number in these fixtures, so a swapped column mapping or a
# double-insert cannot pass the changed-file assertions below.
APPENDED_CALL = (
    '{"type":"assistant","sessionId":"session-fixture",'
    '"timestamp":"2026-07-28T15:20:00.000Z","isSidechain":false,'
    '"message":{"id":"msg_appended","model":"claude-sonnet-5-20260115",'
    '"usage":{"input_tokens":3,"cache_creation_input_tokens":5,'
    '"cache_read_input_tokens":11,"output_tokens":13}}}\n'
)
APPENDED = {"i": 3, "cw": 5, "cr": 11, "o": 13}
APPENDED_SUBAGENT_CALL = (
    '{"type":"assistant","sessionId":"session-fixture","agentId":"agent-atest1",'
    '"timestamp":"2026-07-28T15:05:00.000Z","isSidechain":true,"message":'
    '{"id":"msg_sub_appended","model":"claude-sonnet-5-20260115",'
    '"usage":{"input_tokens":17,"cache_creation_input_tokens":19,'
    '"cache_read_input_tokens":23,"output_tokens":29}}}\n'
)


class SingleFileIngestTest(unittest.TestCase):
    """`--transcript <path>`: ingest exactly ONE file, for hook-driven ingest.

    A Claude Code hook is handed the path of the transcript that just changed,
    so re-scanning the whole tree is pure waste -- measured against a local
    transcript corpus of 2,891 files on 2026-08-04: 1.18-2.44 s for a no-op
    directory scan, 4.09 s when one file had changed.

    The load-bearing constraint is what this mode must NOT do. It looks at one
    file, so it has no basis whatsoever to conclude anything about any other
    source: it must never archive, prune or rewrite rows it did not look at.
    Marking a whole corpus as gone because one hook fired would be exactly the
    silent, unrecoverable loss the archive rules exist to prevent.
    """

    MAIN = {"i": 118, "cw": 233, "cr": 456, "o": 76, "n": 5}
    SUB = {"i": 1030, "cw": 2060, "cr": 4090, "o": 620, "n": 2}

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cpb-single-file-test-"))
        self.projects_dir, self.tasks_dir = build_corpus(self.tmp)
        self.main_path = self.projects_dir / "session-fixture.jsonl"
        subs = self.projects_dir / "session-fixture" / "subagents"
        self.agent1_path = subs / "agent-atest1.jsonl"
        self.agent2_path = subs / "agent-atest2.jsonl"
        # A SECOND main-thread source, so "the file I was pointed at" and
        # "every other source in this DB" are distinguishable.
        self.other_path = self.projects_dir / "second-session.jsonl"
        shutil.copy(FIXTURE, self.other_path)
        self.db_path = self.tmp / "usage.db"
        # A DB that already holds a full corpus -- the state a hook actually
        # fires into.
        self.baseline = ingest(
            self.projects_dir, self.db_path, tasks_dir=self.tasks_dir
        )
        self.fresh_db = self.tmp / "fresh.db"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def rows(self, db: Path, sql: str, *params) -> list:
        conn = sqlite3.connect(db)
        try:
            conn.row_factory = sqlite3.Row
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def q1(self, db: Path, sql: str, *params) -> sqlite3.Row:
        found = self.rows(db, sql, *params)
        self.assertTrue(found, f"query returned no row: {sql}")
        return found[0]

    def append(self, path: Path, record: str) -> None:
        """Make a source genuinely CHANGED, so the next ingest re-parses it.

        Every "leaves everything else alone" assertion below has to run against
        the INGEST path, not the skip path -- a test that silently exercises
        the skip path proves nothing about the writes, and one of these did
        until a mutation caught it.
        """
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(record)

    def runs(self, db: Path) -> list:
        return [tuple(r) for r in self.rows(
            db, "SELECT * FROM subagent_runs ORDER BY agent_id")]

    # ---- the two source kinds -------------------------------------------

    def test_main_thread_transcript_is_ingested_alone(self) -> None:
        summary = ingest_mod.ingest_transcript(self.main_path, self.fresh_db)
        self.assertEqual(summary["files_ingested"], 1)
        row = self.q1(
            self.fresh_db,
            "SELECT COUNT(*) n, SUM(input_tokens) i, SUM(cache_write) cw,"
            " SUM(cache_read) cr, SUM(output_tokens) o FROM api_calls",
        )
        self.assertEqual(
            (row["n"], row["i"], row["cw"], row["cr"], row["o"]),
            (self.MAIN["n"], self.MAIN["i"], self.MAIN["cw"], self.MAIN["cr"],
             self.MAIN["o"]),
        )
        # EXACTLY that file: the sibling subagent transcripts and the second
        # main transcript sit right there on disk and must not be swept in.
        self.assertEqual(
            [r["source_path"] for r in self.rows(
                self.fresh_db, "SELECT DISTINCT source_path FROM api_calls")],
            [str(self.main_path)],
        )
        self.assertEqual(
            self.q1(self.fresh_db, "SELECT COUNT(*) n FROM ingest_state")["n"], 1
        )

    def test_subagent_transcript_is_ingested_alone(self) -> None:
        summary = ingest_mod.ingest_transcript(
            self.agent1_path, self.fresh_db, tasks_dir=self.tasks_dir
        )
        self.assertEqual(summary["files_ingested"], 1)
        row = self.q1(
            self.fresh_db,
            "SELECT COUNT(*) n, SUM(input_tokens) i, SUM(cache_write) cw,"
            " SUM(cache_read) cr, SUM(output_tokens) o, SUM(is_sidechain) side"
            " FROM api_calls",
        )
        self.assertEqual(
            (row["n"], row["i"], row["cw"], row["cr"], row["o"]),
            (self.SUB["n"], self.SUB["i"], self.SUB["cw"], self.SUB["cr"],
             self.SUB["o"]),
        )
        self.assertEqual(row["side"], self.SUB["n"])
        call = self.q1(
            self.fresh_db,
            "SELECT source_kind, agent_id, session_id, turn_id FROM api_calls LIMIT 1",
        )
        # The identity comes from the PATH, exactly as the directory globs
        # derive it: never the `agent-<id>` filename as a session.
        self.assertEqual(call["source_kind"], "subagent")
        self.assertEqual(call["agent_id"], "atest1")
        self.assertEqual(call["session_id"], "session-fixture")
        self.assertIsNone(call["turn_id"])
        # A subagent transcript has no turns of its own.
        self.assertEqual(
            self.q1(self.fresh_db, "SELECT COUNT(*) n FROM turns")["n"], 0
        )

    def test_subagent_run_row_is_recorded_for_the_ingested_agent(self) -> None:
        ingest_mod.ingest_transcript(
            self.agent1_path, self.fresh_db, tasks_dir=self.tasks_dir
        )
        run = self.q1(
            self.fresh_db, "SELECT * FROM subagent_runs WHERE agent_id = 'atest1'"
        )
        self.assertEqual(run["status"], "ingested")
        self.assertEqual(run["agent_type"], "laravel-expert")
        self.assertEqual(run["description"], "Fix widget")

    def test_subagent_calls_follow_the_DISPATCHING_session(self) -> None:
        # atest2 is STORED under session-fixture but DISPATCHED by
        # other-session. Directory mode charges the dispatcher; single-file
        # mode must agree, or which session pays depends on which command
        # happened to ingest the file -- and an unchanged file is SKIPPED
        # forever afterwards, so the wrong attribution would never be revised.
        ingest_mod.ingest_transcript(
            self.agent2_path, self.fresh_db, tasks_dir=self.tasks_dir
        )
        self.assertEqual(
            self.q1(self.fresh_db, "SELECT session_id s FROM api_calls")["s"],
            "other-session",
        )

    def test_without_a_task_index_attribution_falls_back_to_the_store(self) -> None:
        # No index -> the dispatcher is UNKNOWN, so the storing directory is
        # used and said so, rather than a dispatcher being invented.
        ingest_mod.ingest_transcript(
            self.agent2_path, self.fresh_db, tasks_dir=self.tmp / "absent"
        )
        self.assertEqual(
            self.q1(self.fresh_db, "SELECT session_id s FROM api_calls")["s"],
            "session-fixture",
        )
        self.assertIsNone(
            self.q1(
                self.fresh_db,
                "SELECT dispatching_session_id d FROM subagent_runs"
                " WHERE agent_id = 'atest2'",
            )["d"]
        )

    # ---- incremental + idempotent ---------------------------------------

    def test_running_twice_in_a_row_does_not_duplicate_rows(self) -> None:
        ingest_mod.ingest_transcript(self.main_path, self.fresh_db)
        before = tuple(self.q1(
            self.fresh_db,
            "SELECT (SELECT COUNT(*) FROM api_calls) a,"
            " (SELECT COUNT(*) FROM turns) t,"
            " (SELECT COUNT(*) FROM agent_dispatches) d,"
            " (SELECT COUNT(*) FROM ingest_state) s,"
            " (SELECT COUNT(*) FROM sessions) e",
        ))
        second = ingest_mod.ingest_transcript(self.main_path, self.fresh_db)
        after = tuple(self.q1(
            self.fresh_db,
            "SELECT (SELECT COUNT(*) FROM api_calls) a,"
            " (SELECT COUNT(*) FROM turns) t,"
            " (SELECT COUNT(*) FROM agent_dispatches) d,"
            " (SELECT COUNT(*) FROM ingest_state) s,"
            " (SELECT COUNT(*) FROM sessions) e",
        ))
        self.assertEqual(before, after)
        # Skipped, not merely deduplicated on the way in: an unchanged file
        # must not be re-parsed at all, which is the whole point of the mode.
        self.assertEqual(second["files_skipped"], 1)
        self.assertEqual(second["files_ingested"], 0)
        self.assertEqual(second["records_parsed"], 0)

    def test_changed_transcript_is_reparsed_without_duplication(self) -> None:
        ingest_mod.ingest_transcript(self.main_path, self.fresh_db)
        with open(self.main_path, "a", encoding="utf-8") as fh:
            fh.write(APPENDED_CALL)
        summary = ingest_mod.ingest_transcript(self.main_path, self.fresh_db)
        self.assertEqual(summary["files_ingested"], 1)
        row = self.q1(
            self.fresh_db,
            "SELECT COUNT(*) n, SUM(input_tokens) i, SUM(cache_write) cw,"
            " SUM(cache_read) cr, SUM(output_tokens) o FROM api_calls",
        )
        self.assertEqual(row["n"], self.MAIN["n"] + 1)
        self.assertEqual(row["i"], self.MAIN["i"] + APPENDED["i"])
        self.assertEqual(row["cw"], self.MAIN["cw"] + APPENDED["cw"])
        self.assertEqual(row["cr"], self.MAIN["cr"] + APPENDED["cr"])
        self.assertEqual(row["o"], self.MAIN["o"] + APPENDED["o"])
        # ...and the turns did not double either.
        self.assertEqual(
            self.q1(self.fresh_db, "SELECT COUNT(*) n FROM turns")["n"], 3
        )

    def test_unparsed_records_are_still_counted_not_swallowed(self) -> None:
        summary = ingest_mod.ingest_transcript(self.main_path, self.fresh_db)
        self.assertEqual(summary["unparsed_records"], 1)
        self.assertEqual(len(summary["unparsed_details"]), 1)
        self.assertEqual(
            self.q1(self.fresh_db, "SELECT unparsed_records u FROM ingest_state")["u"],
            1,
        )

    # ---- refusals --------------------------------------------------------

    def test_a_nonexistent_path_is_an_error_not_a_silent_no_op(self) -> None:
        missing = self.projects_dir / "no-such-session.jsonl"
        with self.assertRaises(SystemExit) as caught:
            ingest_mod.ingest_transcript(missing, self.fresh_db)
        self.assertIn(str(missing), str(caught.exception))

    def test_a_directory_is_an_error(self) -> None:
        with self.assertRaises(SystemExit):
            ingest_mod.ingest_transcript(self.projects_dir, self.fresh_db)

    def test_a_transcript_that_grows_while_it_is_read_is_re_ingested(self) -> None:
        """The hook fires on a file Claude Code is still writing.

        `store_source()` used to take its own `stat()` AFTER the parse, so a
        transcript appended to mid-read was recorded at its NEW size while only
        the older prefix had been parsed. Every later run then saw "unchanged"
        and skipped it, and the appended calls were lost silently and forever.
        Recording the PRE-read stat makes the worst case one redundant reparse.
        """
        real_parse = ingest_mod.parse_file

        def parse_then_grow(path, collect_turns=True):
            result = real_parse(path, collect_turns=collect_turns)
            self.append(Path(path), APPENDED_CALL)  # the writer, mid-read
            return result

        ingest_mod.parse_file = parse_then_grow
        try:
            ingest_mod.ingest_transcript(self.main_path, self.fresh_db)
        finally:
            ingest_mod.parse_file = real_parse

        second = ingest_mod.ingest_transcript(self.main_path, self.fresh_db)
        self.assertEqual(
            second["files_ingested"], 1,
            "the record appended during the read must still be pending, not "
            "recorded as already ingested",
        )
        self.assertEqual(
            self.q1(self.fresh_db, "SELECT COUNT(*) n FROM api_calls")["n"],
            self.MAIN["n"] + 1,
        )

    def test_a_subagent_path_with_no_session_directory_is_refused(self) -> None:
        # Reachable with a RELATIVE path -- `subagents/agent-x.jsonl` has no
        # session directory above it, and storing its calls under a session id
        # of "" would invent a session that never existed.
        with self.assertRaises(ValueError):
            ingest_mod.source_for_transcript(Path("subagents/agent-x.jsonl"))
        # ...while the same shape WITH a session directory is accepted, so the
        # guard cannot pass by rejecting everything.
        self.assertEqual(
            ingest_mod.source_for_transcript(
                Path("sess-1/subagents/agent-x.jsonl")
            ).session_id,
            "sess-1",
        )

    def test_an_unrecognisable_file_is_an_error(self) -> None:
        notes = self.projects_dir / "notes.txt"
        notes.write_text("not a transcript\n", encoding="utf-8")
        with self.assertRaises(SystemExit) as caught:
            ingest_mod.ingest_transcript(notes, self.fresh_db)
        self.assertIn(str(notes), str(caught.exception))

    # ---- the highest-risk property: everything else is untouched ---------

    def test_it_archives_nothing_it_did_not_look_at(self) -> None:
        """One file cannot be evidence about any other file.

        Directory mode archives every tracked source that is no longer on
        disk. Single-file mode sees ONE path; concluding from it that the
        other 2,890 sources have vanished would mark a whole corpus as gone.
        """
        self.other_path.unlink()  # a source directory mode WOULD archive
        self.append(self.main_path, APPENDED_CALL)
        summary = ingest_mod.ingest_transcript(
            self.main_path, self.db_path, tasks_dir=self.tasks_dir
        )
        self.assertEqual(
            summary["files_ingested"], 1, "must exercise the INGEST path"
        )
        state = self.q1(
            self.db_path,
            "SELECT archived_at, size FROM ingest_state WHERE path = ?",
            str(self.other_path),
        )
        self.assertIsNone(
            state["archived_at"],
            "single-file mode did not look at this source and must make no "
            "claim about it",
        )
        self.assertEqual(
            self.q1(
                self.db_path,
                "SELECT COUNT(*) n FROM api_calls WHERE source_path = ?",
                str(self.other_path),
            )["n"],
            self.MAIN["n"],
            "rows for an untouched source must survive verbatim",
        )
        # The fixture must not make the defect undetectable: prove the deleted
        # file really IS in the state directory mode reacts to, so the NULL
        # above is single-file mode declining to conclude, not a no-op corpus.
        later = ingest(self.projects_dir, self.db_path, tasks_dir=self.tasks_dir)
        self.assertEqual(later["files_archived"], 1)

    def test_it_prunes_nothing_it_did_not_look_at(self) -> None:
        # Deliberately on the SKIP path (the sibling above covers the ingest
        # path): both must be safe, and a hook fires on unchanged files too.
        self.other_path.unlink()
        summary = ingest_mod.ingest_transcript(
            self.main_path, self.db_path, tasks_dir=self.tasks_dir
        )
        self.assertEqual(summary["files_skipped"], 1, "must exercise the SKIP path")
        self.assertEqual(
            self.q1(
                self.db_path, "SELECT COUNT(*) n FROM ingest_state WHERE path = ?",
                str(self.other_path),
            )["n"],
            1,
        )

    def test_it_leaves_every_other_sources_rows_byte_identical(self) -> None:
        def snapshot() -> list:
            return [
                tuple(r) for r in self.rows(
                    self.db_path,
                    "SELECT * FROM api_calls WHERE source_path != ?"
                    " ORDER BY id", str(self.main_path),
                )
            ] + [
                tuple(r) for r in self.rows(
                    self.db_path,
                    "SELECT * FROM ingest_state WHERE path != ? ORDER BY path",
                    str(self.main_path),
                )
            ]

        before = snapshot()
        self.assertTrue(before, "fixture must contain other sources to protect")
        self.append(self.main_path, APPENDED_CALL)
        summary = ingest_mod.ingest_transcript(
            self.main_path, self.db_path, tasks_dir=self.tasks_dir
        )
        self.assertEqual(summary["files_ingested"], 1, "must exercise the INGEST path")
        self.assertEqual(before, snapshot())

    def test_ingesting_a_main_transcript_does_not_wipe_the_subagent_ledger(self) -> None:
        # `store_subagent_runs()` rebuilds `subagent_runs` and
        # `task_index_sessions` WHOLESALE from the sources of that run. Reusing
        # it here would delete every dispatch the one file does not mention --
        # including `agone`, whose whole purpose is to record spend that can
        # no longer be measured.
        before = self.runs(self.db_path)
        self.assertEqual(len(before), 3, "atest1, atest2 and the reaped agone")
        self.append(self.main_path, APPENDED_CALL)
        summary = ingest_mod.ingest_transcript(
            self.main_path, self.db_path, tasks_dir=self.tasks_dir
        )
        self.assertEqual(summary["files_ingested"], 1, "must exercise the INGEST path")
        self.assertEqual(before, self.runs(self.db_path))
        self.assertEqual(
            self.q1(
                self.db_path,
                "SELECT status s FROM subagent_runs WHERE agent_id = 'agone'",
            )["s"],
            "unavailable",
        )
        self.assertEqual(
            self.q1(self.db_path, "SELECT COUNT(*) n FROM task_index_sessions")["n"],
            2,
        )

    def test_ingesting_one_subagent_leaves_the_other_dispatches_alone(self) -> None:
        # The same hazard from the other side: this run DOES write a
        # `subagent_runs` row, and only its own.
        before = {r[0]: r for r in self.runs(self.db_path)}
        self.append(self.agent1_path, APPENDED_SUBAGENT_CALL)
        summary = ingest_mod.ingest_transcript(
            self.agent1_path, self.db_path, tasks_dir=self.tasks_dir
        )
        self.assertEqual(summary["files_ingested"], 1, "must exercise the INGEST path")
        after = {r[0]: r for r in self.runs(self.db_path)}
        self.assertEqual(set(before), set(after), "no dispatch gained or lost")
        for agent_id in ("atest2", "agone"):
            with self.subTest(agent_id=agent_id):
                self.assertEqual(before[agent_id], after[agent_id])
        self.assertEqual(
            self.q1(self.db_path, "SELECT COUNT(*) n FROM task_index_sessions")["n"],
            2,
        )

    def test_other_sessions_survive_the_sessions_rebuild(self) -> None:
        before = [tuple(r) for r in self.rows(
            self.db_path, "SELECT * FROM sessions ORDER BY id")]
        self.assertEqual(len(before), 3, "session-fixture, second-session, other-session")
        self.append(self.agent1_path, APPENDED_SUBAGENT_CALL)
        summary = ingest_mod.ingest_transcript(
            self.agent1_path, self.db_path, tasks_dir=self.tasks_dir
        )
        self.assertEqual(summary["files_ingested"], 1, "must exercise the INGEST path")
        after = [tuple(r) for r in self.rows(
            self.db_path, "SELECT * FROM sessions ORDER BY id")]
        # Only session-fixture's own footprint moved (it grew by the appended
        # record); the other two sessions must be untouched.
        self.assertEqual(
            [r for r in before if r[0] != "session-fixture"],
            [r for r in after if r[0] != "session-fixture"],
        )

    def test_summary_omits_the_counters_for_checks_it_never_ran(self) -> None:
        # Reporting `files_archived: 0` would state that nothing was found
        # missing -- a measurement this mode never took. Absent is not zero.
        summary = ingest_mod.ingest_transcript(self.main_path, self.fresh_db)
        self.assertNotIn("files_archived", summary)
        self.assertNotIn("files_pruned", summary)


class SingleFileIngestCliTest(unittest.TestCase):
    """The CLI contract a hook depends on: flags, precedence and EXIT CODES.

    Exercised as a real subprocess, because the exit status is the whole
    interface -- a hook that swallows a failure into a success makes a broken
    ingest invisible forever.
    """

    INGEST = str(Path(__file__).resolve().parent.parent / "ingest.py")

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cpb-single-file-cli-test-"))
        self.projects_dir = self.tmp / "projects"
        self.projects_dir.mkdir()
        self.main_path = self.projects_dir / "session-fixture.jsonl"
        shutil.copy(FIXTURE, self.main_path)
        self.db_path = self.tmp / "usage.db"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_ingest(self, *args: str, env: dict | None = None):
        environ = dict(os.environ)
        environ.pop("CPB_DB", None)
        environ.update(env or {})
        return subprocess.run(
            [sys.executable, self.INGEST, *args],
            capture_output=True, text=True, env=environ, cwd=str(self.tmp),
        )

    def call_count(self, db: Path) -> int:
        conn = sqlite3.connect(db)
        try:
            return conn.execute("SELECT COUNT(*) FROM api_calls").fetchone()[0]
        finally:
            conn.close()

    def test_exit_code_is_zero_and_the_rows_land(self) -> None:
        proc = self.run_ingest(
            "--transcript", str(self.main_path), "--db", str(self.db_path)
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.call_count(self.db_path), 5)

    def test_the_summary_line_names_the_file_and_the_counts(self) -> None:
        proc = self.run_ingest(
            "--transcript", str(self.main_path), "--db", str(self.db_path)
        )
        self.assertIn(str(self.main_path), proc.stdout)
        self.assertIn("ingested: 1", proc.stdout)
        self.assertIn("records parsed:", proc.stdout)

    def test_a_missing_transcript_exits_non_zero(self) -> None:
        proc = self.run_ingest(
            "--transcript", str(self.tmp / "gone.jsonl"), "--db", str(self.db_path)
        )
        self.assertNotEqual(proc.returncode, 0)

    def test_transcript_and_projects_dir_are_mutually_exclusive(self) -> None:
        proc = self.run_ingest(
            "--transcript", str(self.main_path),
            "--projects-dir", str(self.projects_dir),
            "--db", str(self.db_path),
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("--transcript", proc.stderr)
        self.assertFalse(
            self.db_path.exists(), "a usage error must not half-ingest anything"
        )

    def test_CPB_DB_points_ingest_at_its_own_database(self) -> None:
        env_db = self.tmp / "plugin-data" / "usage.db"
        proc = self.run_ingest(
            "--transcript", str(self.main_path), env={"CPB_DB": str(env_db)}
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(env_db.exists(), "CPB_DB must be honoured, including mkdir")
        self.assertEqual(self.call_count(env_db), 5)

    def test_the_db_flag_beats_the_env_var(self) -> None:
        env_db = self.tmp / "env.db"
        proc = self.run_ingest(
            "--transcript", str(self.main_path), "--db", str(self.db_path),
            env={"CPB_DB": str(env_db)},
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.call_count(self.db_path), 5)
        self.assertFalse(env_db.exists(), "--db must win outright, not merely first")

    def test_CPB_DB_applies_to_directory_mode_too(self) -> None:
        # One resolution order for the whole CLI: a plugin that exports CPB_DB
        # must not find that a full re-ingest silently writes somewhere else.
        env_db = self.tmp / "both-modes.db"
        proc = self.run_ingest(
            "--projects-dir", str(self.projects_dir), env={"CPB_DB": str(env_db)}
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.call_count(env_db), 5)

    def test_help_documents_the_env_var(self) -> None:
        proc = self.run_ingest("--help")
        self.assertEqual(proc.returncode, 0)
        self.assertIn("CPB_DB", proc.stdout)

    def test_resolve_db_path_precedence(self) -> None:
        default = Path("/default/usage.db")
        self.assertEqual(
            ingest_mod.resolve_db_path(Path("/flag.db"), "/env.db", default),
            Path("/flag.db"),
        )
        self.assertEqual(
            ingest_mod.resolve_db_path(None, "/env.db", default), Path("/env.db")
        )
        self.assertEqual(ingest_mod.resolve_db_path(None, None, default), default)

    def test_an_empty_CPB_DB_refuses_rather_than_falling_back(self) -> None:
        # Falling back would write the measurements to a DIFFERENT database
        # than the operator configured, and say nothing about it.
        with self.assertRaises(SystemExit):
            ingest_mod.resolve_db_path(None, "   ", Path("/default/usage.db"))


if __name__ == "__main__":
    unittest.main()
