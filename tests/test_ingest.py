"""Tests for the usage-report ingest pipeline (#4948).

Fixture design note (CLAUDE.md "fixture must not make the defect undetectable"):
every token class in the fixture carries a DELIBERATELY UNEQUAL value, so a
swapped column mapping (e.g. cache_read <-> cache_write) cannot pass.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
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
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

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

FIXTURES = Path(__file__).resolve().parent / "fixtures"
FIXTURE = FIXTURES / "session-fixture.jsonl"
SUBAGENT_FIXTURE = (
    Path(__file__).resolve().parent / "fixtures" / "subagent-transcript.jsonl"
)


def approve_this_prune(plan: "ingest_mod.PrunePlan") -> bool:
    """A caller who read the plan and said yes (#92).

    Spelled at every call site that wants a prune, never defaulted, because
    that is exactly the property `ingest()` now has: consent is an argument
    somebody passed, and `prune_missing=True` on its own deletes nothing. A
    test that reached the deletion by omitting an argument would be testing
    the behaviour this change exists to remove.
    """
    return True


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

    def test_api_calls_stores_no_derived_money_column(self) -> None:
        # #30: the list-rate estimate is gone from the schema, not merely
        # hidden from the API. A column that still existed would be a stale
        # figure waiting to be re-surfaced by the next reader of the DB.
        columns = {
            r[1] for r in self.conn.execute("PRAGMA table_info(api_calls)")
        }
        self.assertNotIn("cost_usd", columns)
        # ...and the token columns it was derived from are all still there.
        self.assertLessEqual(
            {"input_tokens", "cache_read", "cache_write", "output_tokens"}, columns
        )

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
        summary2 = ingest(
            self.projects_dir,
            self.db_path,
            prune_missing=True,
            approve_prune=approve_this_prune,
        )
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
        summary2 = ingest(
            self.projects_dir,
            self.db_path,
            prune_missing=True,
            approve_prune=approve_this_prune,
        )
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

    def test_per_agent_rollup_carries_tokens_and_model(self) -> None:
        row = self.q1(
            "SELECT agent_id, COUNT(*) calls, SUM(cache_read) cr,"
            " SUM(input_tokens + cache_read + cache_write + output_tokens) tokens"
            " FROM api_calls WHERE agent_id = 'atest1' GROUP BY agent_id"
        )
        self.assertEqual(row["calls"], 2)
        self.assertEqual(row["cr"], 4090)
        self.assertGreater(row["tokens"], row["cr"])

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
        # #92 added a second lock rather than replacing this one: asking for
        # the prune (`prune_missing`) and consenting to the censused plan
        # (`approve_prune`) are now two acts, and both are needed here.
        before = self.calls()
        self.transcript.unlink()
        ingest(
            self.projects, self.db,
            prune_missing=True, approve_prune=approve_this_prune,
        )
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


class PruneShowsItsWorkTest(unittest.TestCase):
    """The census a prune is approved from ranges over the rows it deletes (#92).

    `--prune-missing` is the one operation in CPB that destroys measurements
    nothing can regenerate: past Claude Code's `cleanupPeriodDays` the rows are
    the only surviving copy of that history, which is why the default is
    archive-not-delete and why `_prepare_schema()` refuses a rebuild that would
    drop them. Opt-in protects against the default; it does not protect against
    a typo, so the flag now states what it will delete and asks.

    The failure mode this class exists for is NOT "the confirmation was
    skipped" -- that is the easy half, pinned by `PruneConfirmationTest`. It is
    the one this repository keeps rediscovering: a summary computed by one
    query and a deletion performed by another, free to range over different
    sets, agreeing only by the author's care. `RANKED_BY`'s heading and its
    `ORDER BY` disagreed for a whole release for exactly that reason.

    So `plan_prune()` counts a path list and `execute_prune()` deletes that
    same `PrunePlan`'s OWN path list, through the same `PRUNE_TABLES` iteration
    with the same key columns. These tests pin the tie rather than the two
    halves separately: a count that ranged wider than the delete would report a
    loss that did not happen, and one that ranged narrower would understate a
    loss that did -- and the second is how somebody types the word over a
    summary that hid half of what went.
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cpb-prune-plan-test-"))
        self.projects = self.tmp / "projects"
        self.projects.mkdir()
        # THREE sources, and the fixture must not make the defect
        # undetectable: two go, one stays. A one-source corpus cannot tell a
        # plan scoped to the reaped set from one scoped to the whole database,
        # which is the confusion with teeth here.
        self.doomed = [self.projects / "gone-a.jsonl", self.projects / "gone-b.jsonl"]
        self.kept = self.projects / "still-here.jsonl"
        for path in (*self.doomed, self.kept):
            shutil.copy(FIXTURE, path)
        self.db = self.tmp / "usage.db"
        ingest(self.projects, self.db)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db)

    def rows_for(self, path: Path) -> dict[str, int]:
        conn = self.connect()
        try:
            return ingest_mod.census_source_rows(conn, str(path))
        finally:
            conn.close()

    def total_rows(self) -> dict[str, int]:
        conn = self.connect()
        try:
            return {
                table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table, _ in ingest_mod.PRUNE_TABLES
            }
        finally:
            conn.close()

    def reap(self) -> None:
        for path in self.doomed:
            path.unlink()

    def test_a_prune_nobody_approved_deletes_nothing_and_archives_instead(self) -> None:
        # THE default that matters. `prune_missing=True` says a prune was
        # ASKED for; it is not consent to one, and a caller who forgets the
        # approver must land in the safe state rather than the destructive one.
        self.reap()
        before = self.total_rows()
        summary = ingest(self.projects, self.db, prune_missing=True)
        self.assertEqual(self.total_rows(), before, "nothing may have been deleted")
        self.assertEqual(summary["files_pruned"], 0)
        self.assertEqual(
            summary["files_archived"], 2,
            "a refused prune must still ARCHIVE -- `archived_at` is what tells "
            "the report its totals are no longer reproducible",
        )
        self.assertIs(summary["prune_approved"], False)

    def test_a_run_that_never_asked_censuses_nothing(self) -> None:
        # Tri-state, and the third state is the one absence-is-never-a-value
        # requires: a run with no `--prune-missing` says NOTHING about what a
        # prune would cost, which is different from having found nothing to
        # prune. `None`, never an empty plan.
        self.reap()
        summary = ingest(self.projects, self.db)
        self.assertIsNone(summary["prune_plan"])
        self.assertIsNone(summary["prune_approved"])

    def test_the_plan_is_censused_before_a_single_row_is_deleted(self) -> None:
        # Ordering is the whole guarantee. A summary computed after the delete
        # would be a report of an empty set, and the approver would be shown
        # nothing to withhold consent from.
        self.reap()
        before = self.total_rows()
        seen: list = []

        def approve(plan: ingest_mod.PrunePlan) -> bool:
            seen.append(plan)
            self.assertEqual(
                self.total_rows(), before,
                "the approver was called AFTER rows were already deleted",
            )
            return True

        ingest(self.projects, self.db, prune_missing=True, approve_prune=approve)
        self.assertEqual(len(seen), 1)
        self.assertLess(self.total_rows()["api_calls"], before["api_calls"])

    def test_the_summary_counts_exactly_the_rows_the_delete_removes(self) -> None:
        # THE tie, asserted as arithmetic rather than as a shape. Per table,
        # because a plan that got the total right by counting one table wide
        # and another narrow would pass a check on the sum.
        self.reap()
        before = self.total_rows()
        captured: list = []

        def approve(plan: ingest_mod.PrunePlan) -> bool:
            captured.append(plan)
            return True

        ingest(self.projects, self.db, prune_missing=True, approve_prune=approve)
        after = self.total_rows()
        plan = captured[0]
        for table, _ in ingest_mod.PRUNE_TABLES:
            with self.subTest(table=table):
                self.assertEqual(
                    before[table] - after[table],
                    plan.rows_by_table[table],
                    f"{table}: the plan promised a different number of rows "
                    f"than the deletion took",
                )
        self.assertEqual(sum(plan.rows_by_table.values()), plan.rows)

    def test_the_census_names_every_table_the_delete_touches(self) -> None:
        # The other half of the tie, and the one a new table would break: a
        # table added to `delete_source_rows()` and not to the census would be
        # rows destroyed that no summary ever mentioned. They read the same
        # tuple, so this asserts the tuple IS what the delete uses.
        deleted_from = set()
        conn = self.connect()
        try:
            conn.set_trace_callback(
                lambda sql: deleted_from.add(sql.split()[2])
                if sql.strip().upper().startswith("DELETE FROM")
                else None
            )
            ingest_mod.delete_source_rows(conn, str(self.kept))
            conn.rollback()
        finally:
            conn.set_trace_callback(None)
            conn.close()
        self.assertEqual(
            deleted_from,
            {table for table, _ in ingest_mod.PRUNE_TABLES},
            "the delete touches a table the census does not count, or vice versa",
        )

    def test_the_plan_ranges_over_the_reaped_set_and_not_the_database(self) -> None:
        # The set the plan names is the set that goes. The surviving source is
        # the control: a plan scoped to the whole database would list it, and a
        # delete scoped that way would take it.
        self.reap()
        kept_before = self.rows_for(self.kept)
        captured: list = []
        ingest(
            self.projects, self.db,
            prune_missing=True,
            approve_prune=lambda plan: (captured.append(plan), True)[1],
        )
        self.assertEqual(
            sorted(captured[0].paths), sorted(str(p) for p in self.doomed)
        )
        self.assertEqual(captured[0].sources, 2)
        self.assertEqual(
            self.rows_for(self.kept), kept_before,
            "a source still on disk must not be touched by a prune",
        )

    def test_execute_prune_deletes_the_plans_own_paths_and_no_others(self) -> None:
        # `execute_prune()` takes a plan, not a path list, so the only way to
        # reach the deletion is through a set somebody was shown. Handed a plan
        # naming ONE source, it must take that one.
        conn = self.connect()
        try:
            plan = ingest_mod.plan_prune(conn, self.db, [str(self.doomed[0])])
            other_before = self.rows_for(self.doomed[1])
            self.assertEqual(ingest_mod.execute_prune(conn, plan), 1)
        finally:
            conn.close()
        self.assertEqual(
            self.rows_for(self.doomed[0]),
            {table: 0 for table, _ in ingest_mod.PRUNE_TABLES},
        )
        self.assertEqual(self.rows_for(self.doomed[1]), other_before)

    def test_the_plan_names_the_date_range_of_the_calls_involved(self) -> None:
        # A count says how much history goes; only a range says WHICH history.
        # Pinned against the reaped sources' own MIN/MAX rather than the
        # database's, so a range computed over every call would fail even
        # though both are "a real date range".
        self.reap()
        conn = self.connect()
        try:
            plan = ingest_mod.plan_prune(conn, self.db, [str(p) for p in self.doomed])
            lo, hi, n = conn.execute(
                "SELECT MIN(ts), MAX(ts), COUNT(ts) FROM api_calls"
                " WHERE source_path IN (?, ?)",
                tuple(str(p) for p in self.doomed),
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual((plan.first_call_ts, plan.last_call_ts), (lo, hi))
        self.assertEqual(plan.dated_calls, n)
        rendered = ingest_mod.format_prune_plan(plan)
        self.assertIn("date range of the calls involved", rendered)
        self.assertIn(ingest_mod._fmt_prune_ts(lo), rendered)
        self.assertIn(ingest_mod._fmt_prune_ts(hi), rendered)

    def test_an_undated_call_is_counted_apart_from_the_range(self) -> None:
        # `api_calls.ts` is nullable, so the range ranges over a strictly
        # smaller set than the rows being deleted -- and must say so. A range
        # drawn from 2 of 10 calls presented as covering all 10 is an aggregate
        # that does not name the set it ranges over.
        self.reap()
        conn = self.connect()
        try:
            conn.execute(
                "UPDATE api_calls SET ts = NULL WHERE source_path = ?",
                (str(self.doomed[0]),),
            )
            conn.commit()
            plan = ingest_mod.plan_prune(conn, self.db, [str(p) for p in self.doomed])
        finally:
            conn.close()
        calls = plan.rows_by_table["api_calls"]
        self.assertLess(plan.dated_calls, calls)
        self.assertIsNotNone(plan.first_call_ts)
        rendered = ingest_mod.format_prune_plan(plan)
        self.assertIn(f"{plan.dated_calls} of {calls} call(s)", rendered)
        self.assertIn(f"{calls - plan.dated_calls} carry none", rendered)

    def test_a_set_with_no_dated_call_reads_as_unknown_not_as_empty(self) -> None:
        # Absence is never rendered as a value, in the field most tempting to
        # default: a missing range must not print as "1970-01-01" or as a
        # blank, both of which read as an answer.
        self.reap()
        conn = self.connect()
        try:
            conn.execute("UPDATE api_calls SET ts = NULL")
            conn.commit()
            plan = ingest_mod.plan_prune(conn, self.db, [str(p) for p in self.doomed])
        finally:
            conn.close()
        self.assertIsNone(plan.first_call_ts)
        self.assertIsNone(plan.last_call_ts)
        self.assertEqual(plan.dated_calls, 0)
        self.assertGreater(plan.rows_by_table["api_calls"], 0)
        rendered = ingest_mod.format_prune_plan(plan)
        self.assertIn("UNKNOWN", rendered)
        self.assertIn("Unknown, not empty.", rendered)
        self.assertNotIn("1970", rendered)

    def test_a_set_with_no_calls_at_all_is_a_different_sentence(self) -> None:
        # "No call rows are in this set" and "no call row carries a date" are
        # two facts, and collapsing them would tell a reader deleting only
        # turn rows that their dates were unreadable.
        conn = self.connect()
        try:
            plan = ingest_mod.plan_prune(conn, self.db, [])
        finally:
            conn.close()
        self.assertEqual(plan.rows_by_table["api_calls"], 0)
        rendered = ingest_mod.format_prune_plan(plan)
        self.assertIn("no api_calls row is in this set", rendered)
        self.assertNotIn("UNKNOWN", rendered)

    def test_the_plan_states_the_database_it_would_delete_from(self) -> None:
        # The guard travels with whatever `--db` or CPB_DB resolved to, so the
        # summary has to name it: a plan that showed counts without saying
        # which database they came from is one a reader cannot check against
        # the file they meant.
        conn = self.connect()
        try:
            plan = ingest_mod.plan_prune(conn, self.db, [str(self.kept)])
        finally:
            conn.close()
        self.assertIn(str(self.db), ingest_mod.format_prune_plan(plan))
        self.assertIn(str(self.kept), ingest_mod.format_prune_plan(plan))


class FakeTerminal:
    """A stdin somebody is sitting at, and the answer they type.

    Not `io.StringIO`: its `isatty()` is False, which is the very condition the
    refusal path turns on, so a test built on one could never reach the prompt.
    """

    def __init__(self, answer: str, *, interactive: bool = True) -> None:
        self.answer = answer
        self.interactive = interactive
        self.reads = 0

    def isatty(self) -> bool:
        return self.interactive

    def readline(self) -> str:
        self.reads += 1
        return self.answer


class UnaskableStream:
    """A stream that raises when asked whether it is a terminal."""

    def isatty(self) -> bool:
        raise ValueError("I/O operation on closed file")

    def readline(self) -> str:  # pragma: no cover -- must never be reached
        raise AssertionError("a stream that cannot be asked must not be read")


class PruneConfirmationTest(unittest.TestCase):
    """Consent to the deletion is obtained, never assumed (#92).

    `PruneConfirmation` is the only place the answer is decided and the only
    place the plan is printed, which is what makes `--dry-run` and a real run
    show byte-identical summaries: one call to `format_prune_plan()` over one
    `PrunePlan`. Every path sets `outcome`, because "nothing was deleted" is
    several different facts and the exit status turns on which.
    """

    def plan(self, sources: int = 2) -> ingest_mod.PrunePlan:
        return ingest_mod.PrunePlan(
            db_path="/tmp/usage.db",
            paths=tuple(f"/tmp/gone-{i}.jsonl" for i in range(sources)),
            # Deliberately unequal per table, so a formatter that printed one
            # count in every slot cannot pass.
            rows_by_table={"api_calls": 41, "turns": 7, "agent_dispatches": 3,
                           "ingest_state": 2, "source_shape": 5},
            dated_calls=41,
            first_call_ts=1_780_000_000.0,
            last_call_ts=1_785_000_000.0,
        )

    def ask(self, **kwargs) -> tuple[bool, ingest_mod.PruneConfirmation, str]:
        out: list[str] = []
        confirmation = ingest_mod.PruneConfirmation(out=out.append, **kwargs)
        answer = confirmation(self.plan())
        return answer, confirmation, "\n".join(out)

    def test_the_plan_is_printed_before_the_question_is_asked(self) -> None:
        terminal = FakeTerminal("delete\n")
        approved, confirmation, printed = self.ask(
            assume_yes=False, dry_run=False, stdin=terminal
        )
        self.assertTrue(approved)
        self.assertIs(confirmation.outcome, ingest_mod.PRUNE_CONFIRMED)
        self.assertLess(
            printed.index("PRUNE PLAN"),
            printed.index(ingest_mod.PRUNE_CONFIRM_WORD + " to delete"),
            "the question was asked before the plan was shown",
        )

    def test_the_summary_states_the_sources_the_rows_and_the_range(self) -> None:
        # All three of the issue's requirements in one assertion set: a count
        # with no date range does not tell someone what they are about to lose.
        _, _, printed = self.ask(assume_yes=True, dry_run=False)
        self.assertIn("sources no longer on disk: 2", printed)
        self.assertIn("rows to delete: 58", printed)
        for table, count in (("api_calls", 41), ("turns", 7),
                             ("agent_dispatches", 3), ("ingest_state", 2),
                             ("source_shape", 5)):
            with self.subTest(table=table):
                self.assertIn(f"{table}: {count}", printed)
        self.assertIn("date range of the calls involved", printed)
        self.assertIn("/tmp/gone-0.jsonl", printed)
        self.assertIn("/tmp/usage.db", printed)

    def test_the_summary_says_the_rows_may_be_the_only_copy(self) -> None:
        # The durability argument is the reason this prompt exists, so it is
        # part of the prompt rather than part of the commit message.
        _, _, printed = self.ask(assume_yes=True, dry_run=False)
        self.assertIn("cleanupPeriodDays", printed)
        self.assertIn("ONLY", printed)

    def test_typing_the_word_is_what_approves(self) -> None:
        terminal = FakeTerminal(f"{ingest_mod.PRUNE_CONFIRM_WORD}\n")
        approved, confirmation, _ = self.ask(
            assume_yes=False, dry_run=False, stdin=terminal
        )
        self.assertTrue(approved)
        self.assertEqual(terminal.reads, 1)
        self.assertIs(confirmation.outcome, ingest_mod.PRUNE_CONFIRMED)

    def test_anything_but_the_word_declines(self) -> None:
        # Including "y" and "yes": a single reflexive keystroke is not consent
        # to deleting the only copy of a month of history.
        for answer in ("y\n", "yes\n", "\n", "DELETE\n", " del \n", "no\n"):
            with self.subTest(answer=answer):
                approved, confirmation, printed = self.ask(
                    assume_yes=False, dry_run=False,
                    stdin=FakeTerminal(answer),
                )
                self.assertFalse(approved)
                self.assertIs(confirmation.outcome, ingest_mod.PRUNE_DECLINED)
                self.assertIn("NOTHING", printed)

    def test_surrounding_whitespace_does_not_defeat_the_word(self) -> None:
        approved, _, _ = self.ask(
            assume_yes=False, dry_run=False,
            stdin=FakeTerminal(f"  {ingest_mod.PRUNE_CONFIRM_WORD}  \n"),
        )
        self.assertTrue(approved)

    def test_a_non_interactive_stdin_refuses_rather_than_proceeding(self) -> None:
        # A hook or a pipe cannot be asked, and "cannot be asked" is not "said
        # yes". The plan is still printed, so the operator's log says exactly
        # what the refusal was about.
        approved, confirmation, printed = self.ask(
            assume_yes=False, dry_run=False,
            stdin=FakeTerminal("delete\n", interactive=False),
        )
        self.assertFalse(approved)
        self.assertIs(confirmation.outcome, ingest_mod.PRUNE_NOT_INTERACTIVE)
        self.assertIn("PRUNE PLAN", printed)
        self.assertIn("REFUSED", printed)
        self.assertIn(confirmation.outcome, ingest_mod.PRUNE_REFUSALS)

    def test_a_non_interactive_stdin_is_never_even_read(self) -> None:
        # Teeth on the test above. Reading the pipe and comparing the word
        # would let `echo delete | ingest.py --prune-missing` delete -- which
        # is the scripted path `--yes` exists to make explicit.
        terminal = FakeTerminal("delete\n", interactive=False)
        self.ask(assume_yes=False, dry_run=False, stdin=terminal)
        self.assertEqual(terminal.reads, 0)

    def test_end_of_input_is_not_an_answer(self) -> None:
        # A terminal that closes mid-prompt (Ctrl-D) reads as EOF, and a
        # readline() of "" treated as agreement would be the non-interactive
        # hole with one extra step.
        approved, confirmation, printed = self.ask(
            assume_yes=False, dry_run=False, stdin=FakeTerminal(""),
        )
        self.assertFalse(approved)
        self.assertIs(confirmation.outcome, ingest_mod.PRUNE_NO_ANSWER)
        self.assertIn(confirmation.outcome, ingest_mod.PRUNE_REFUSALS)
        self.assertIn("NOTHING was deleted", printed)

    def test_a_stream_that_cannot_be_asked_is_not_a_yes(self) -> None:
        approved, confirmation, _ = self.ask(
            assume_yes=False, dry_run=False, stdin=UnaskableStream(),
        )
        self.assertFalse(approved)
        self.assertIs(confirmation.outcome, ingest_mod.PRUNE_NOT_INTERACTIVE)

    def test_yes_approves_without_asking(self) -> None:
        terminal = FakeTerminal("no\n")
        approved, confirmation, printed = self.ask(
            assume_yes=True, dry_run=False, stdin=terminal
        )
        self.assertTrue(approved)
        self.assertIs(confirmation.outcome, ingest_mod.PRUNE_ASSUMED_YES)
        self.assertEqual(terminal.reads, 0)
        self.assertIn("PRUNE PLAN", printed)

    def test_dry_run_approves_nothing_and_is_not_a_refusal(self) -> None:
        approved, confirmation, printed = self.ask(
            assume_yes=False, dry_run=True, stdin=FakeTerminal("delete\n")
        )
        self.assertFalse(approved)
        self.assertIs(confirmation.outcome, ingest_mod.PRUNE_DRY_RUN)
        self.assertNotIn(
            confirmation.outcome, ingest_mod.PRUNE_REFUSALS,
            "a dry run did exactly what it was asked; it is not a refusal",
        )
        self.assertIn("NOTHING was deleted", printed)

    def test_the_dry_run_and_the_real_run_print_the_SAME_summary(self) -> None:
        # The issue's requirement, and it is structural rather than kept in
        # step: both paths render one `format_prune_plan()` over one plan, so
        # this compares the whole block byte for byte. A second formatter for
        # the preview -- the obvious way to write this feature -- would be a
        # second description of the set, free to differ from the one somebody
        # actually approves.
        plan = self.plan()
        outputs = {}
        for name, kwargs in (
            ("dry", {"assume_yes": False, "dry_run": True}),
            ("real", {"assume_yes": True, "dry_run": False}),
        ):
            captured: list[str] = []
            ingest_mod.PruneConfirmation(out=captured.append, **kwargs)(plan)
            outputs[name] = captured[0]
        self.assertEqual(outputs["dry"], outputs["real"])
        self.assertIn("PRUNE PLAN", outputs["dry"])

    def test_an_empty_plan_is_not_a_deletion_to_approve(self) -> None:
        confirmation = ingest_mod.PruneConfirmation(
            assume_yes=True, dry_run=False, out=lambda _line: None
        )
        empty = ingest_mod.PrunePlan(
            db_path="/tmp/usage.db", paths=(),
            rows_by_table={table: 0 for table, _ in ingest_mod.PRUNE_TABLES},
            dated_calls=0, first_call_ts=None, last_call_ts=None,
        )
        self.assertFalse(confirmation(empty))
        self.assertIs(confirmation.outcome, ingest_mod.PRUNE_NOTHING_MISSING)

    def test_stdin_is_resolved_when_asked_and_not_at_construction(self) -> None:
        # A confirmation built before `sys.stdin` was redirected must ask the
        # stream that is current when the question is put, or a harness that
        # rebinds stdin would be asking a stream nobody types into.
        confirmation = ingest_mod.PruneConfirmation(
            assume_yes=False, dry_run=False, out=lambda _line: None
        )
        with mock.patch.object(ingest_mod.sys, "stdin", FakeTerminal("delete\n")):
            self.assertTrue(confirmation(self.plan()))


class PruneCliTest(unittest.TestCase):
    """The guard as a user meets it: flags, output and exit status (#92).

    Subprocess rather than in-process, because the exit status is part of the
    governed CLI surface (`docs/versioning.md` clause 1) and `main()` returning
    None where it should refuse is invisible to an in-process call. Every run
    below sets stdin explicitly -- inheriting the test runner's would make the
    interactive branch depend on whether somebody ran the suite from a
    terminal.
    """

    INGEST = str(Path(__file__).resolve().parent.parent / "ingest.py")

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cpb-prune-cli-test-"))
        self.projects = self.tmp / "projects"
        self.projects.mkdir()
        self.transcript = self.projects / "session-fixture.jsonl"
        shutil.copy(FIXTURE, self.transcript)
        self.db = self.tmp / "usage.db"
        self.run_ingest("--projects-dir", str(self.projects), "--db", str(self.db))
        self.before = self.calls()
        self.assertGreater(self.before, 0, "the fixture must produce rows")
        self.transcript.unlink()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_ingest(self, *args: str, env: dict | None = None, stdin: str = ""):
        environ = dict(os.environ)
        environ.pop("CPB_DB", None)
        environ["PYTHONDONTWRITEBYTECODE"] = "1"
        environ.update(env or {})
        return subprocess.run(
            [sys.executable, "-B", self.INGEST, *args],
            capture_output=True, text=True, env=environ, input=stdin,
            cwd=str(self.tmp),
        )

    def calls(self) -> int:
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute("SELECT COUNT(*) FROM api_calls").fetchone()[0]
        finally:
            conn.close()

    def archived(self) -> int:
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute(
                "SELECT COUNT(*) FROM ingest_state WHERE archived_at IS NOT NULL"
            ).fetchone()[0]
        finally:
            conn.close()

    def prune(self, *extra: str, env: dict | None = None):
        return self.run_ingest(
            "--projects-dir", str(self.projects), "--db", str(self.db),
            "--prune-missing", *extra, env=env,
        )

    def test_a_pipe_without_yes_refuses_and_keeps_every_row(self) -> None:
        # `input=""` gives the child a PIPE, which is what a hook, a cron job
        # and `ingest.py ... | tee` all hand it. The state change asserted is
        # the rows, not the message.
        proc = self.prune()
        self.assertEqual(self.calls(), self.before, proc.stdout)
        self.assertEqual(self.archived(), 1)
        self.assertIn("PRUNE PLAN", proc.stdout)
        self.assertIn("REFUSED", proc.stdout)

    def test_a_refusal_does_not_exit_zero(self) -> None:
        # The user asked to delete and nothing was deleted. Reporting 0 would
        # say "pruned nothing" in the same voice as "there was nothing to
        # prune" -- `cpb._exit_status()` exists to keep those apart.
        self.assertNotEqual(self.prune().returncode, 0)

    def test_yes_deletes_and_still_shows_its_work(self) -> None:
        proc = self.prune("--yes")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.calls(), 0)
        self.assertIn("PRUNE PLAN", proc.stdout)
        self.assertIn("pruned (deleted): 1", proc.stdout)

    def test_dry_run_prints_the_summary_and_deletes_nothing(self) -> None:
        proc = self.prune("--dry-run")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.calls(), self.before)
        self.assertEqual(self.archived(), 1)
        self.assertIn("PRUNE PLAN", proc.stdout)
        self.assertIn("pruned (deleted): 0", proc.stdout)

    def test_the_dry_run_summary_is_the_summary_the_deletion_shows(self) -> None:
        # End to end, over a real database rather than a hand-built plan: the
        # block a user reads before deciding is the block that describes what
        # actually goes.
        preview = self.prune("--dry-run").stdout
        real = self.prune("--yes").stdout
        block = preview[preview.index("PRUNE PLAN"):preview.index("--dry-run:")]
        self.assertIn(block.strip(), real)

    def test_the_guard_applies_to_a_database_named_by_CPB_DB(self) -> None:
        # The guard travels with the database, not with the default path: an
        # env-resolved DB is the plugin's normal case (`${CLAUDE_PLUGIN_DATA}`)
        # and is exactly where the only surviving copy lives.
        proc = self.run_ingest(
            "--projects-dir", str(self.projects), "--prune-missing",
            env={"CPB_DB": str(self.db)},
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("REFUSED", proc.stdout)
        self.assertEqual(self.calls(), self.before)

    def test_yes_without_prune_missing_is_a_usage_error(self) -> None:
        # A flag that silently does nothing is the CLI's form of absence
        # rendered as a value: the user stated an intent about a deletion and
        # there is no deletion for it to be about.
        for flag in ("--yes", "--dry-run"):
            with self.subTest(flag=flag):
                proc = self.run_ingest(
                    "--projects-dir", str(self.projects), "--db", str(self.db),
                    flag,
                )
                self.assertEqual(proc.returncode, 2, proc.stderr)
                self.assertIn("without --prune-missing", proc.stderr)

    def test_yes_and_dry_run_together_are_a_usage_error(self) -> None:
        # One approves the deletion and the other performs none. Picking a
        # winner would make a typo silently destructive in one direction.
        proc = self.prune("--yes", "--dry-run")
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertEqual(self.calls(), self.before)

    def test_the_flags_are_documented_where_the_prune_is(self) -> None:
        proc = self.run_ingest("--help")
        self.assertEqual(proc.returncode, 0)
        for flag in ("--prune-missing", "--yes", "--dry-run"):
            with self.subTest(flag=flag):
                self.assertIn(flag, proc.stdout)
        self.assertIn(ingest_mod.PRUNE_CONFIRM_WORD, proc.stdout)


class HookNeverPrunesTest(unittest.TestCase):
    """The one caller that must never reach the destructive path (#92).

    `hooks/cpb_ingest_hook.py` fires on `SubagentStop`, `Stop` and `SessionEnd`
    -- unattended, with no terminal, many times a session. It is the single
    worst place for a prune to be reachable, and it is also the one place a
    future edit would add a flag without a human watching the output.

    Pinned three ways, because each catches a different edit: the flag appears
    nowhere in the file, `build_argv()` does not emit it, and `ingest.py`
    rejects the combination the hook's own mode would produce anyway.
    """

    HOOK = Path(__file__).resolve().parent.parent / "hooks" / "cpb_ingest_hook.py"

    def test_the_flag_appears_nowhere_in_the_hook(self) -> None:
        self.assertNotIn("--prune-missing", self.HOOK.read_text(encoding="utf-8"))

    def test_the_argv_the_hook_builds_carries_no_prune_flag(self) -> None:
        # Loaded by PATH, matching `tests/test_plugin_hook.py`: `hooks/` is not
        # on `sys.path` and an `import cpb_ingest_hook` statement would be an
        # unresolvable name to the `stdlib-only` CI job, which ASTs every
        # shipped module and knows only the repo root and `tests/` as local.
        spec = importlib.util.spec_from_file_location("cpb_ingest_hook", self.HOOK)
        hook = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(hook)
        argv = hook.build_argv(Path("/tmp/session.jsonl"))
        self.assertIn("--transcript", argv)
        for flag in ("--prune-missing", "--yes"):
            with self.subTest(flag=flag):
                self.assertNotIn(flag, argv)

    def test_single_file_mode_rejects_the_prune_flag_outright(self) -> None:
        # Belt and braces, and it is the mode the hook always runs in: even a
        # hook that grew the flag could not delete, because a run that examined
        # ONE file has no basis to conclude any other source is gone.
        proc = subprocess.run(
            [sys.executable, "-B", str(self.HOOK.parent.parent / "ingest.py"),
             "--transcript", "/tmp/nope.jsonl", "--prune-missing"],
            capture_output=True, text=True, input="",
            env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
        )
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("cannot be combined with --transcript", proc.stderr)


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

    The resolution rule is not a guess: the record with the GREATEST
    `output_tokens` survives, ties going to the last. It reads like "last
    wins" here only because this fixture's sequence rises (2 -> 2 -> 450), as
    the real corpus almost always does -- 26,998 of 27,106 multi-record ids
    were non-decreasing when re-measured 2026-08-05, against 4,928 of 4,928 on
    the older corpus the rule was derived from. Almost is not always, and
    nothing here can tell the two rules apart; NonMonotonicOutputDedupeTest
    below is what defends the difference (#49). First-wins would have
    undercounted output by ~99%.
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


def _epoch(iso: str) -> float:
    """Epoch seconds for a UTC ISO8601 stamp, computed independently of ingest."""
    return datetime.fromisoformat(iso).replace(tzinfo=timezone.utc).timestamp()


class NonMonotonicOutputDedupeTest(unittest.TestCase):
    """The GREATEST `output_tokens` survives, ties going to the LAST record (#49).

    The rule is `max`, not "last wins", and the difference is load-bearing.
    `output_tokens` rising monotonically across an id's records is a strong
    empirical TENDENCY, not an invariant: over the local corpus of 49
    main-thread transcripts on 2026-08-05, 26,998 of 27,106 multi-record ids
    were non-decreasing (99.60%) and 108 were not. On those 108 a literal
    last-record rule selects a SMALLER output than a record already seen --
    understating finished totals by 107,810 output tokens in aggregate, up to
    6,858 on a single id. That is #2's inflation defect running in reverse, so
    `max` is kept and this test defends it.

    The corpus is a growing sample, not a constant: the derivation in #2 found
    4,928 of 4,928 (100%) on a smaller and older corpus, which is why "last
    wins" was an accurate shorthand when it was written and is not one now.
    Re-measure before quoting the ratio; better, do not depend on it -- `max`
    does not.

    Fixture design (CLAUDE.md: a fixture must not make the defect
    undetectable). The two older dedupe fixtures are both non-decreasing
    (`streamed-message.jsonl` 2 -> 2 -> 450; `divergent-usage.jsonl` 59 ->
    263), so `max` and `last` select the SAME record in every one of their
    assertions and neither can see the rule at all.
    `non-monotonic-output.jsonl` rises then falls -- 7 -> 4000 -> 12 -- so the
    two rules disagree by 3,988 tokens, and holds every other usage field
    equal across the records so this pins SELECTION alone; the divergence
    path has its own test below.
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cpb-nonmono-test-"))
        self.projects = self.tmp / "projects"
        self.projects.mkdir(parents=True)
        shutil.copy(
            FIXTURES / "non-monotonic-output.jsonl", self.projects / "n.jsonl"
        )
        self.db = self.tmp / "usage.db"
        self.summary = ingest(self.tmp / "projects", self.db)
        self.conn = sqlite3.connect(self.db)
        self.conn.row_factory = sqlite3.Row

    def tearDown(self) -> None:
        self.conn.close()
        shutil.rmtree(self.tmp)

    def test_two_ids_are_two_rows(self) -> None:
        # Guards the assertions below against passing vacuously on a row that
        # dedupe never produced.
        n = self.conn.execute("SELECT COUNT(*) FROM api_calls").fetchone()[0]
        self.assertEqual(n, 2, "one row per message.id: msg_peak + msg_tie")

    def test_a_FALLING_sequence_keeps_the_GREATEST_output_not_the_last(self) -> None:
        # 4000 (the peak), never 12 (the last record), 7 (the first) or 4019
        # (the sum). All four are deliberately unequal so no two can be
        # confused. `group[-1]` -- the "simplification" the old docs invited
        # -- selects 12 here and fails.
        row = self.conn.execute(
            "SELECT output_tokens FROM api_calls WHERE message_id = 'msg_peak'"
        ).fetchone()
        self.assertEqual(row["output_tokens"], 4000)

    def test_the_survivor_is_ONE_WHOLE_record_not_a_synthesis(self) -> None:
        # Every field must come from the SAME record -- the peak one, at
        # :02 -- so a future per-field maximum cannot pass. The timestamp is
        # the discriminator: it is the only thing distinguishing the three
        # records apart from output, and it is not part of the selection key.
        row = self.conn.execute(
            "SELECT ts, input_tokens, cache_read, cache_write, output_tokens"
            " FROM api_calls WHERE message_id = 'msg_peak'"
        ).fetchone()
        self.assertEqual(
            (
                row["ts"],
                row["input_tokens"],
                row["cache_read"],
                row["cache_write"],
                row["output_tokens"],
            ),
            (_epoch("2026-07-18T10:00:02"), 13, 9100, 800, 4000),
        )

    def test_EQUAL_maxima_resolve_to_the_LATER_record(self) -> None:
        # What `reversed()` is for, and otherwise silent: `max()` is stable
        # and returns the FIRST maximum, so plain `max(group, ...)` would keep
        # the :05 record instead of the :06 one. Ties are the COMMON case --
        # every record before the final one repeats the same usage -- so this
        # decides which record most rows in the DB actually are.
        row = self.conn.execute(
            "SELECT ts, output_tokens FROM api_calls WHERE message_id = 'msg_tie'"
        ).fetchone()
        self.assertEqual(row["output_tokens"], 500)
        self.assertEqual(
            row["ts"],
            _epoch("2026-07-18T10:00:06"),
            "on a tie the LAST record wins, not the first maximum",
        )

    def test_totals_use_the_peak_not_the_final_record(self) -> None:
        total = self.conn.execute(
            "SELECT SUM(output_tokens) FROM api_calls"
        ).fetchone()[0]
        self.assertEqual(total, 4500, "4000 + 500; last-wins would say 512")

    def test_this_fixture_pins_SELECTION_not_divergence(self) -> None:
        # The two are independent code paths and the fixture holds every
        # non-output usage field equal, so a decreasing sequence alone must
        # not trip the divergence counter. (On the real corpus the two sets
        # coincided on 2026-08-05 -- see the class docstring -- but that is a
        # dated observation about one corpus, not a property of the rule.)
        self.assertEqual(self.summary["divergent_message_ids"], 0)


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
            "must be one record verbatim, never a per-field maximum -- here"
            " the greatest-output record, which is also the last one",
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
    above). Both are right for a shape change. Neither is right for a delta
    that touches no row: a rebuild would discard every row to re-derive
    identical ones, and on a corpus with a reaped transcript the guard would
    refuse outright -- making the change unlandable for exactly the users whose
    data this database is the only copy of.

    v6 -> v8 is now TWO such deltas at once: the added `ingest_runs` table
    (#20) and the dropped `api_calls.cost_usd` column (#30). Neither deletes a
    row, so the whole hop still upgrades in place -- see
    `CostColumnRemovalUpgradeTest` for the column half and for the fallback
    taken when SQLite is too old to drop a column.

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
        """Put the database back in the shape v6 shipped.

        THREE differences from the current shape, not one: v6 had no run stamp
        table and no shape census table, and it still carried the
        `api_calls.cost_usd` estimate. Restore ALL of them, or the fixture
        tests a hop that no real database ever made.
        """
        conn = sqlite3.connect(self.db)
        try:
            conn.execute(f"DROP TABLE {ingest_mod.INGEST_RUNS_TABLE}")
            conn.execute(f"DROP TABLE {ingest_mod.SHAPE_TABLE}")
            conn.execute("ALTER TABLE api_calls ADD COLUMN cost_usd REAL")
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
class DropColumnCapabilityTest(unittest.TestCase):
    """`ALTER TABLE ... DROP COLUMN` is DETECTED, never assumed (#30).

    It landed in SQLite 3.35 (2021-03), and the version a Python interpreter
    ships is the platform's business, not ours -- a developer laptop's 3.53 says
    nothing about a CI runner or a distro Python. Assuming it would turn an
    upgrade into an `OperationalError` on exactly the machines we cannot see.

    The parser is deliberately strict-in / conservative-out: anything it cannot
    read as a version becomes "not supported", so an unrecognized string takes
    the slower rebuild path instead of attempting a statement that may not
    exist. That is this repo's own rule -- an unreadable input is not a
    permission.
    """

    def test_the_documented_floor_is_supported_and_the_release_below_it_is_not(
        self,
    ) -> None:
        self.assertTrue(ingest_mod._sqlite_supports_drop_column("3.35.0"))
        self.assertFalse(ingest_mod._sqlite_supports_drop_column("3.34.1"))
        # The boundary from the other side: a 3.34.x with a large patch level
        # is still older than 3.35.0, which a naive string compare gets wrong.
        self.assertFalse(ingest_mod._sqlite_supports_drop_column("3.34.100"))

    def test_a_two_component_version_is_read_as_its_zero_patch_release(self) -> None:
        # Tuple comparison alone reads "3.35" as (3, 35) < (3, 35, 0) and would
        # reject a supported build.
        self.assertTrue(ingest_mod._sqlite_supports_drop_column("3.35"))
        self.assertFalse(ingest_mod._sqlite_supports_drop_column("3.34"))

    def test_an_unreadable_version_is_not_supported(self) -> None:
        # "4" is the case that gives the `len(parsed) < 2` guard teeth. The
        # other single-component string here, "3", is rejected by the version
        # comparison anyway -- padded to (3, 0, 0) it is already below the
        # (3, 35, 0) floor -- so the guard could be DELETED with the suite
        # green. "4" pads to (4, 0, 0), which clears the floor: without the
        # guard a string saying nothing whatever about a minor release reads
        # as a permission to run DROP COLUMN.
        for value in ("", "not-a-version", "3.x.1", "3", "4"):
            with self.subTest(value=value):
                self.assertFalse(ingest_mod._sqlite_supports_drop_column(value))

    def test_the_default_argument_reads_the_live_runtime_version(self) -> None:
        # The check must range over THIS interpreter's SQLite, not a constant
        # baked in at authoring time.
        with mock.patch.object(sqlite3, "sqlite_version", "3.34.1"):
            self.assertFalse(ingest_mod._sqlite_supports_drop_column())
        with mock.patch.object(sqlite3, "sqlite_version", "3.35.0"):
            self.assertTrue(ingest_mod._sqlite_supports_drop_column())


class CostColumnRemovalUpgradeTest(unittest.TestCase):
    """v7 -> v8 drops `api_calls.cost_usd` without touching a single row (#30).

    The estimate is being removed from the schema, and the databases it has to
    be removed from may be the ONLY copy of their measurements: past Claude
    Code's `cleanupPeriodDays` window the transcripts are gone. So the upgrade
    is held to a stronger standard than "the tests still pass" -- every
    surviving column of every surviving row must be byte-identical afterwards,
    and the reaped-source guard must be untouched underneath it.

    `ALTER TABLE ... DROP COLUMN` (SQLite 3.35+) rewrites the table's own
    storage in place: no rebuild, no DROP TABLE, so the guard is never met
    because nothing it protects is at risk. Where SQLite is too old the change
    is NOT forced through -- it falls back to the ordinary rebuild path, guard
    included, which refuses over a reaped source exactly as it always has.
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cpb-cost-drop-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.projects = self.tmp / "projects"
        self.projects.mkdir()
        self.transcript = self.projects / "session-fixture.jsonl"
        shutil.copy(FIXTURE, self.transcript)
        self.db = self.tmp / "usage.db"
        ingest(self.projects, self.db)

    # -- helpers ---------------------------------------------------------
    def _columns(self, table: str = "api_calls") -> list[str]:
        conn = sqlite3.connect(self.db)
        try:
            return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
        finally:
            conn.close()

    def _user_version(self) -> int:
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute("PRAGMA user_version").fetchone()[0]
        finally:
            conn.close()

    def _snapshot(self) -> list[tuple]:
        """Every api_calls row, every column EXCEPT the one being dropped.

        Whole rows rather than a COUNT: the claim under test is "nothing but
        that column changed", and a row count cannot tell a preserved value
        from a re-derived or shifted one.
        """
        columns = [c for c in self._columns() if c != "cost_usd"]
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute(
                f"SELECT {', '.join(columns)} FROM api_calls ORDER BY id"
            ).fetchall()
        finally:
            conn.close()

    def _downgrade_to_v7(self) -> None:
        """Restore the shape v7 shipped: `cost_usd` present and populated, and
        no shape census table (#15 added that at v9).

        The values are distinct per row and deliberately not derivable from the
        token columns, so a "migration" that silently rebuilt the table from
        the transcripts would be visible as a changed snapshot rather than
        landing on the same numbers by luck.
        """
        conn = sqlite3.connect(self.db)
        try:
            conn.execute("ALTER TABLE api_calls ADD COLUMN cost_usd REAL")
            conn.execute("UPDATE api_calls SET cost_usd = id * 0.25 + 0.125")
            conn.execute(f"DROP TABLE {ingest_mod.SHAPE_TABLE}")
            conn.execute("PRAGMA user_version = 7")
            conn.commit()
        finally:
            conn.close()

    # -- the in-place path ------------------------------------------------
    def test_a_fresh_database_has_no_cost_column_at_all(self) -> None:
        self.assertNotIn("cost_usd", self._columns())
        self.assertEqual(self._user_version(), ingest_mod.SCHEMA_VERSION)

    def test_v7_upgrade_drops_the_column_and_changes_nothing_else(self) -> None:
        self._downgrade_to_v7()
        self.assertIn("cost_usd", self._columns())
        before = self._snapshot()
        self.assertTrue(before, "the fixture must hold rows for this to mean anything")

        summary = ingest(self.projects, self.db)

        self.assertFalse(summary["schema_rebuilt"])
        # A rebuild empties `ingest_state`, so every file would be re-parsed.
        # Skipping them all is the observable proof the rows were carried, not
        # re-derived.
        self.assertEqual(summary["files_ingested"], 0)
        self.assertEqual(summary["files_skipped"], summary["files_scanned"])
        self.assertNotIn("cost_usd", self._columns())
        self.assertEqual(self._user_version(), ingest_mod.SCHEMA_VERSION)
        self.assertEqual(self._snapshot(), before)

    def test_v7_upgrade_is_not_refused_over_a_reaped_source(self) -> None:
        # The case that makes a DROP-and-rebuild migration unshippable: on any
        # corpus older than the retention window the guard fires and the user
        # is told to stay on the old version forever.
        self.transcript.unlink()
        ingest(self.projects, self.db)  # archives it; rows retained
        self._downgrade_to_v7()
        before = self._snapshot()

        ingest(self.projects, self.db)  # must not raise SystemExit

        self.assertNotIn("cost_usd", self._columns())
        self.assertEqual(self._snapshot(), before)
        conn = sqlite3.connect(self.db)
        try:
            archived = conn.execute(
                "SELECT COUNT(*) FROM ingest_state WHERE archived_at IS NOT NULL"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(archived, 1, "the archived source survived the upgrade")

    def test_v6_upgrade_adds_the_run_table_and_drops_the_column_in_one_hop(self) -> None:
        # v6 -> v8 is both deltas at once; a user who skipped v7 must not be
        # left half-upgraded.
        conn = sqlite3.connect(self.db)
        try:
            conn.execute("ALTER TABLE api_calls ADD COLUMN cost_usd REAL")
            conn.execute(f"DROP TABLE {ingest_mod.INGEST_RUNS_TABLE}")
            conn.execute("PRAGMA user_version = 6")
            conn.commit()
        finally:
            conn.close()
        before = self._snapshot()

        summary = ingest(self.projects, self.db)

        self.assertFalse(summary["schema_rebuilt"])
        self.assertNotIn("cost_usd", self._columns())
        self.assertEqual(self._snapshot(), before)
        conn = sqlite3.connect(self.db)
        try:
            self.assertEqual(
                conn.execute(
                    f"SELECT COUNT(*) FROM {ingest_mod.INGEST_RUNS_TABLE}"
                ).fetchone()[0],
                1,
            )
        finally:
            conn.close()

    # -- the fallback, FORCED (never skipped on a new local SQLite) --------
    def test_old_sqlite_falls_back_to_the_rebuild_path(self) -> None:
        # Forced by patching the reported version, not by skipping when the
        # local build happens to be new: a fallback nobody executes is a
        # fallback nobody knows is broken.
        self._downgrade_to_v7()
        with mock.patch.object(sqlite3, "sqlite_version", "3.34.1"):
            summary = ingest(self.projects, self.db)
        self.assertTrue(
            summary["schema_rebuilt"],
            "without DROP COLUMN the only correct route is the rebuild",
        )
        # The rebuild re-derives from the transcripts, so the rows come back
        # (this corpus is fully re-readable) and the column is gone either way.
        self.assertNotIn("cost_usd", self._columns())
        self.assertEqual(self._user_version(), ingest_mod.SCHEMA_VERSION)
        self.assertEqual(summary["files_ingested"], summary["files_scanned"])
        self.assertTrue(self._snapshot(), "the rebuild must re-ingest the rows")

    def test_the_fallback_does_not_route_around_the_reaped_source_guard(self) -> None:
        # The guard is the whole reason the in-place path exists. A fallback
        # that bypassed it would delete the measurements the refusal protects
        # -- worse than the refusal it was written to avoid.
        self.transcript.unlink()
        ingest(self.projects, self.db)  # archives it; rows retained
        self._downgrade_to_v7()
        before = self._snapshot()

        with mock.patch.object(sqlite3, "sqlite_version", "3.34.1"):
            with self.assertRaises(SystemExit) as caught:
                ingest(self.projects, self.db)

        message = str(caught.exception)
        self.assertIn("REFUSING", message)
        self.assertIn(str(self.transcript), message)
        # Refused means REFUSED: the database is untouched, still v7, still
        # carrying the column and every row.
        self.assertEqual(self._user_version(), 7)
        self.assertIn("cost_usd", self._columns())
        self.assertEqual(self._snapshot(), before)


class SchemaVersionSetsAreDecidedTest(unittest.TestCase):
    """The in-place upgrade set names versions whose delta is row-preserving.

    It is not cumulative by habit: a version belongs in it only while every
    difference between its shape and the CURRENT one can be applied without
    losing a measurement. This test cannot judge that for a future version --
    what it pins is that the set was re-read at the bump, by tying it to the
    six hops (#20's added table, #30's dropped column, #15's added census
    table, #36's uniqueness, #5's added diagnostics columns, #84's added
    per-TTL cache-write columns) that were actually reasoned about.
    """

    def test_schema_version_is_12(self) -> None:
        self.assertEqual(ingest_mod.SCHEMA_VERSION, 12)

    def test_only_the_six_reasoned_hops_upgrade_in_place(self) -> None:
        # v9 was re-checked against the CURRENT shape at the v10 bump, not
        # inherited: v9 -> v10 adds the UNIQUE index over `task_id`, which
        # needs the duplicates already stored resolved first. That deletes
        # rows, so the bar this set applies had to be RESTATED at that bump --
        # what the hop preserves is every DISPATCH, not every row.
        #
        # v10 was decided at the v11 bump against the same restated bar: v10 ->
        # v11 adds three nullable `api_calls` columns and deletes nothing, and
        # NULL in them is a true statement about a row written before CPB ever
        # read `message.diagnostics` -- it means unmeasured, which is what those
        # rows are.
        #
        # v11 is decided at THIS bump, and its delta is two more nullable
        # `api_calls` columns (#84). What earns it a place is as much what the
        # hop leaves alone: `cache_write` keeps its meaning and its value, so
        # no figure reading it changes, and the only value written to an
        # existing row is NULL -- "this row predates CPB reading
        # `usage.cache_creation`", which is true of every one of them. The
        # reasoning is written out above the constant.
        self.assertEqual(
            ingest_mod.IN_PLACE_UPGRADE_FROM, frozenset({6, 7, 8, 9, 10, 11})
        )

    def test_the_current_version_is_not_in_the_upgrade_set(self) -> None:
        # A no-op hop is not an upgrade; listing it would make the branch
        # unreachable-but-plausible, which is how a set rots into a rubber stamp.
        self.assertNotIn(
            ingest_mod.SCHEMA_VERSION, ingest_mod.IN_PLACE_UPGRADE_FROM
        )

    def test_the_shipped_schema_creates_no_money_column(self) -> None:
        self.assertNotIn("cost_usd", ingest_mod.SCHEMA)


class PreV5ShortcutIsGatedOnDropColumnTest(unittest.TestCase):
    """The pre-v5 in-place hop refuses to run without DROP COLUMN (#30).

    That branch adds `ingest_state.archived_at` and then stamps
    `user_version = 8`. Reaching v8 also means shedding `api_calls.cost_usd`,
    so the branch is gated on `_sqlite_supports_drop_column()`: on an older
    library it must fall through to the rebuild, which reaches the same shape
    by re-parsing. Ungated, an ancient SQLite would either raise mid-upgrade
    or stamp "v8" on a table still carrying the retired money column -- a
    version number asserting a shape that is not there.

    Pinning this is awkward because the suite's other version tests patch
    `sqlite3.sqlite_version` while the real library happily executes DROP
    COLUMN, so the statement itself cannot be made to fail. This intercepts
    `_drop_retired_cost_column` instead and asserts on WHETHER IT IS REACHED,
    which is the decision the gate actually makes. Both directions are pinned:
    the modern-version half proves the fixture reaches the branch at all, so
    the "not reached" assertion is not passing for want of a code path.
    """

    # The pre-v5 shape of `ingest_state`, STATED rather than derived by
    # dropping `archived_at` off the current one.
    #
    # The first version of this fixture did derive it, and it passed here on
    # SQLite 3.53.3 while failing every CI leg on 3.45.1 with "error in table
    # ingest_state after drop column: incomplete input". `archived_at` is the
    # LAST column of `ingest_state` and the text immediately before it is a
    # four-line `--` comment; DROP COLUMN rewrites the table's stored CREATE
    # text, and where the dropped column is last the closing paren is
    # re-appended at the truncation point -- which on that shape falls inside
    # a commented-out line, so the reconstructed statement never closes.
    # (Measured on 3.53.3: the drop discards the comment block entirely and
    # emits `last_ts REAL)`, which is why the newer library tolerates it. The
    # 3.45.1 mechanism is inferred from the error string, not observed here.)
    #
    # Deriving a historical schema by mutating the current one through an
    # operation with its own version-dependent constraints is what made a test
    # about a version gate itself version-dependent.
    PRE_V5_INGEST_STATE = """
        CREATE TABLE ingest_state (
            path TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            source_kind TEXT NOT NULL,
            size INTEGER NOT NULL,
            mtime REAL NOT NULL,
            unparsed_records INTEGER NOT NULL,
            first_ts REAL,
            last_ts REAL
        )
    """

    def _pre_v5_database(self) -> sqlite3.Connection:
        """A pre-v5 database: no `ingest_state.archived_at`, `cost_usd` present.

        `ingest_state` is left EMPTY so the reaped-source guard upstream has
        nothing to refuse over -- this test is about the gate below it.

        `cost_usd` is added rather than stated because ADD COLUMN only appends
        to the stored CREATE text: it triggers no schema reconstruction and so
        carries none of the version-dependent risk DROP COLUMN does. It is
        also how `CostColumnRemovalUpgradeTest._downgrade_to_v7` builds the
        same column.
        """
        tmp = Path(tempfile.mkdtemp(prefix="cpb-prev5-gate-test-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        conn = sqlite3.connect(tmp / "usage.db")
        self.addCleanup(conn.close)
        ingest_mod._prepare_schema(conn)
        conn.execute("DROP TABLE ingest_state")
        conn.execute(self.PRE_V5_INGEST_STATE)
        conn.execute("ALTER TABLE api_calls ADD COLUMN cost_usd REAL")
        conn.execute("PRAGMA user_version = 4")
        conn.commit()
        columns = ingest_mod._table_columns(conn, "ingest_state")
        # The branch's own preconditions, asserted rather than assumed.
        self.assertNotIn("archived_at", columns)
        self.assertIn("source_kind", columns)
        # ...and the retired column really is there to be shed, so the
        # assertion that the rebuild sheds it is not vacuous.
        self.assertIn("cost_usd", ingest_mod._table_columns(conn, "api_calls"))
        return conn

    def test_a_modern_sqlite_takes_the_shortcut(self) -> None:
        # The control. Without it, the assertion below could pass because the
        # fixture never reaches the branch, not because the gate held.
        conn = self._pre_v5_database()
        with mock.patch.object(sqlite3, "sqlite_version", "3.45.0"), \
                mock.patch.object(ingest_mod, "_drop_retired_cost_column") as drop:
            rebuilt = ingest_mod._prepare_schema(conn)
        self.assertFalse(rebuilt, "the in-place hop should not rebuild")
        drop.assert_called_once()
        self.assertIn(
            "archived_at", ingest_mod._table_columns(conn, "ingest_state")
        )

    def test_an_old_sqlite_falls_through_to_the_rebuild_instead(self) -> None:
        conn = self._pre_v5_database()

        def unreachable(_conn: sqlite3.Connection) -> None:
            raise AssertionError(
                "the pre-v5 shortcut ran on a SQLite without DROP COLUMN"
            )

        with mock.patch.object(sqlite3, "sqlite_version", "3.34.1"), \
                mock.patch.object(
                    ingest_mod, "_drop_retired_cost_column", side_effect=unreachable
                ) as drop:
            rebuilt = ingest_mod._prepare_schema(conn)
        drop.assert_not_called()
        self.assertTrue(rebuilt, "the old-SQLite path must rebuild, not shortcut")
        # The rebuild reaches the SAME shape the shortcut would have: the
        # fall-through is a slower route to v8, not a skipped upgrade.
        self.assertIn(
            "archived_at", ingest_mod._table_columns(conn, "ingest_state")
        )
        self.assertNotIn("cost_usd", ingest_mod._table_columns(conn, "api_calls"))
        self.assertEqual(
            conn.execute("PRAGMA user_version").fetchone()[0],
            ingest_mod.SCHEMA_VERSION,
        )


class SchemaShapeProbeTest(unittest.TestCase):
    """`user_version` is a CLAIM about shape; it must be verified before it is
    stamped (#35).

    Every statement in `SCHEMA` is `CREATE TABLE IF NOT EXISTS`, which accepts
    ANY pre-existing table of that name. So the in-place upgrade -- which
    decided it could stamp from the version NUMBER plus "at least one derived
    table exists" -- would certify a table with the wrong columns, or a table
    it had just created empty, as current. Three shapes were hand-built and
    each was stamped with `rebuilt=False` and exit 0 (measured 2026-08-05).

    The third is the one this project exists to prevent: no exception, no
    banner, `turns` recreated empty while `ingest_state` still marks every
    source unchanged, so the report renders a confident empty "By turn type"
    beside a populated "By model".

    It was also PERMANENT: once stamped, `version == SCHEMA_VERSION` means no
    migration path ever runs again, so the repair the database still needed
    became unreachable. Hence the probe runs at the current version too.
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cpb-shape-probe-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.projects = self.tmp / "projects"
        self.projects.mkdir()
        self.transcript = self.projects / "session-fixture.jsonl"
        shutil.copy(FIXTURE, self.transcript)
        self.db = self.tmp / "usage.db"
        ingest(self.projects, self.db)

    # -- helpers ---------------------------------------------------------
    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db)
        self.addCleanup(conn.close)
        return conn

    def _user_version(self) -> int:
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute("PRAGMA user_version").fetchone()[0]
        finally:
            conn.close()

    def _counts(self) -> dict[str, int]:
        """Row count of every table that survives on disk, by name.

        Counted per table rather than in total: the defect under test recreates
        ONE table empty and leaves the others populated, which a grand total
        would only show as a smaller number of an unnamed set.
        """
        conn = sqlite3.connect(self.db)
        try:
            names = [
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                    " AND name NOT LIKE 'sqlite_%'"
                )
            ]
            return {
                name: conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
                for name in sorted(names)
            }
        finally:
            conn.close()

    def _stamp(self, version: int) -> None:
        conn = self._conn()
        conn.execute(f"PRAGMA user_version = {version}")
        conn.commit()

    def _downgrade_to_v6(self) -> None:
        """The shape v6 shipped: no run-stamp table, `cost_usd` still present."""
        conn = self._conn()
        conn.execute(f"DROP TABLE {ingest_mod.INGEST_RUNS_TABLE}")
        conn.execute("ALTER TABLE api_calls ADD COLUMN cost_usd REAL")
        conn.execute("PRAGMA user_version = 6")
        conn.commit()

    # The pre-v5 shape of `ingest_state`, STATED rather than derived from the
    # current one by DROP COLUMN -- that statement is exactly what fails on
    # SQLite 3.45.1 for a last column preceded by a comment (see
    # `SchemaCommentPlacementTest`), which would make this fixture, not the
    # code under test, the thing that differs across CI legs.
    INGEST_STATE_WITHOUT_ARCHIVED_AT = """
        CREATE TABLE ingest_state_old (
            path TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            source_kind TEXT NOT NULL,
            size INTEGER NOT NULL,
            mtime REAL NOT NULL,
            unparsed_records INTEGER NOT NULL,
            first_ts REAL,
            last_ts REAL
        )
    """

    def _drop_archived_at_keeping_rows(self) -> None:
        conn = self._conn()
        conn.execute(self.INGEST_STATE_WITHOUT_ARCHIVED_AT)
        conn.execute(
            "INSERT INTO ingest_state_old SELECT path, session_id, source_kind,"
            " size, mtime, unparsed_records, first_ts, last_ts FROM ingest_state"
        )
        conn.execute("DROP TABLE ingest_state")
        conn.execute("ALTER TABLE ingest_state_old RENAME TO ingest_state")
        conn.commit()
        self.assertNotIn(
            "archived_at", ingest_mod._table_columns(conn, "ingest_state")
        )

    # -- the three hand-built shapes -------------------------------------
    def test_a_dropped_derived_table_is_refused_not_recreated_empty(self) -> None:
        # The silent one. `turns` is gone; `ingest_state` still says every
        # source is unchanged, so a re-ingest will never re-read them and
        # `api_calls.turn_id` would point at rows that no longer exist.
        self._downgrade_to_v6()
        before = self._counts()
        self.assertGreater(before["turns"], 0, "the fixture must have turns")
        conn = self._conn()
        conn.execute("DROP TABLE turns")
        conn.commit()

        with self.assertRaises(SystemExit) as caught:
            ingest(self.projects, self.db)

        message = str(caught.exception)
        self.assertIn("REFUSING", message)
        # The refusal must NAME the table, or the operator is told only that
        # something is wrong somewhere.
        self.assertIn("turns", message)
        # Refused means REFUSED: no stamp, and nothing else touched.
        self.assertEqual(self._user_version(), 6)
        after = self._counts()
        self.assertNotIn("turns", after)
        self.assertEqual(after, {k: v for k, v in before.items() if k != "turns"})

    def test_a_wrong_shaped_run_table_is_refused_before_it_reaches_serve(
        self,
    ) -> None:
        # `ingest_runs(id, started_at)` satisfies `CREATE TABLE IF NOT EXISTS`
        # and every "has tables" check, and 500s `/api/summary` on the next
        # read with `no such column: finished_at`.
        self._downgrade_to_v6()
        conn = self._conn()
        conn.execute(
            f"CREATE TABLE {ingest_mod.INGEST_RUNS_TABLE}"
            " (id INTEGER PRIMARY KEY, started_at TEXT)"
        )
        conn.commit()
        before = self._counts()

        with self.assertRaises(SystemExit) as caught:
            ingest(self.projects, self.db)

        message = str(caught.exception)
        self.assertIn("REFUSING", message)
        self.assertIn(ingest_mod.INGEST_RUNS_TABLE, message)
        self.assertIn("finished_at", message)
        self.assertEqual(self._user_version(), 6)
        self.assertEqual(self._counts(), before)

    def test_a_missing_archived_at_column_is_repaired_not_stamped_over(
        self,
    ) -> None:
        # The only missing column an in-place upgrade may supply: adding a
        # nullable `archived_at` states the truth about an old row (NULL = the
        # source is still on disk), and the next run recomputes it. Stamping
        # WITHOUT adding it raised `no such column: archived_at` on the next
        # run and left the repair permanently unreachable.
        self._downgrade_to_v6()
        self._drop_archived_at_keeping_rows()
        before = self._counts()

        summary = ingest(self.projects, self.db)

        self.assertFalse(summary["schema_rebuilt"])
        self.assertEqual(summary["files_ingested"], 0, "no source was re-parsed")
        self.assertEqual(self._user_version(), ingest_mod.SCHEMA_VERSION)
        conn = self._conn()
        self.assertIn("archived_at", ingest_mod._table_columns(conn, "ingest_state"))
        after = self._counts()
        # `ingest_runs` is created by the same hop, so compare the tables that
        # existed before rather than the whole dict.
        self.assertEqual({k: after[k] for k in before}, before)

    def test_the_probe_still_catches_a_database_already_stamped_current(
        self,
    ) -> None:
        # The permanence half of #35: a database the unprobed path stamped is
        # now at `version == SCHEMA_VERSION`, where no migration branch runs at
        # all. Without a probe here the damage is undetectable forever.
        conn = self._conn()
        conn.execute("DROP TABLE turns")
        conn.commit()
        self.assertEqual(self._user_version(), ingest_mod.SCHEMA_VERSION)

        with self.assertRaises(SystemExit) as caught:
            ingest(self.projects, self.db)

        self.assertIn("turns", str(caught.exception))

    def test_the_refusal_says_whether_a_rebuild_would_lose_measurements(
        self,
    ) -> None:
        # The advice differs by corpus. Where every source is still on disk the
        # operator can move the database aside and re-ingest; where one has been
        # reaped, that same action destroys the only copy. A refusal that does
        # not distinguish them invites the destructive reading.
        self.transcript.unlink()
        ingest(self.projects, self.db)  # archives it; rows retained
        self._downgrade_to_v6()
        conn = self._conn()
        conn.execute("DROP TABLE turns")
        conn.commit()

        with self.assertRaises(SystemExit) as caught:
            ingest(self.projects, self.db)

        message = str(caught.exception)
        self.assertIn(str(self.transcript), message)
        self.assertEqual(self._user_version(), 6)

    # An `api_calls` from before `message_id` existed (#2), STATED rather than
    # derived from the current one, for the same reason as above.
    API_CALLS_WITHOUT_MESSAGE_ID = """
        CREATE TABLE api_calls (
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
            is_sidechain INTEGER NOT NULL DEFAULT 0
        )
    """

    def test_a_genuinely_old_shape_is_rebuilt_rather_than_refused(self) -> None:
        # The boundary the probe must NOT cross. For a version nobody listed as
        # row-preserving, "the tables differ from the current shape" is not
        # damage -- it is what an old version means -- and the rebuild is the
        # exit written for it. A probe that refused here would strand every
        # genuinely old database in the field, which is a worse outcome than
        # the defect it was added for.
        conn = self._conn()
        conn.execute("DROP TABLE api_calls")
        conn.executescript(self.API_CALLS_WITHOUT_MESSAGE_ID)
        conn.execute("PRAGMA user_version = 1")
        conn.commit()

        summary = ingest(self.projects, self.db)

        self.assertTrue(summary["schema_rebuilt"])
        self.assertEqual(summary["files_ingested"], summary["files_scanned"])
        self.assertEqual(self._user_version(), ingest_mod.SCHEMA_VERSION)
        conn = self._conn()
        self.assertIn("message_id", ingest_mod._table_columns(conn, "api_calls"))
        self.assertGreater(
            self._counts()["api_calls"], 0, "the rebuild must re-derive the rows"
        )

    def test_the_shipped_shape_passes_its_own_probe(self) -> None:
        # The control. Every refusal above is worthless if the probe also
        # refuses the shape this tool writes.
        conn = self._conn()
        self.assertEqual(ingest_mod._shape_problems(conn), [])
        summary = ingest(self.projects, self.db)
        self.assertFalse(summary["schema_rebuilt"])


class SchemaPlanOrderingTest(unittest.TestCase):
    """The order of `_prepare_schema`'s exits is data, not prose (#35).

    Four exits became six, and which one wins when several conditions hold at
    once was explained in a comment and enforced nowhere -- so a future edit
    that reordered the branches would change behaviour that no test describes.
    The decision is now a pure function over the facts, and this is the
    precedence table.

    The reaped-source probe is passed as a CALLABLE so "was it consulted?" is
    observable: the in-place path must reach its decision without asking,
    because that guard protects a REBUILD and applies to nothing this path
    does.
    """

    class _Probe:
        """A reaped-source probe that records whether it was consulted."""

        def __init__(self, unreadable: bool) -> None:
            self.unreadable = unreadable
            self.calls = 0

        def __call__(self) -> bool:
            self.calls += 1
            return self.unreadable

    def _plan(self, probe: "SchemaPlanOrderingTest._Probe", **facts: object) -> str:
        defaults: dict[str, object] = {
            "version": ingest_mod.SCHEMA_VERSION,
            "has_tables": True,
            "shape_problems": [],
            "pending_additions": False,
            "needs_column_drop": False,
            "can_drop_column": True,
        }
        defaults.update(facts)
        return ingest_mod._schema_plan(any_source_unreadable=probe, **defaults)

    def test_a_fresh_database_is_simply_created(self) -> None:
        probe = self._Probe(False)
        self.assertEqual(
            self._plan(probe, has_tables=False, version=0), ingest_mod.PLAN_CURRENT
        )
        self.assertEqual(probe.calls, 0)

    def test_a_shape_mismatch_outranks_every_other_exit(self) -> None:
        # Listed version, reaped source, a column to drop: the shape refusal
        # wins over all of them, and reaches that verdict without touching the
        # filesystem.
        probe = self._Probe(True)
        self.assertEqual(
            self._plan(
                probe,
                version=6,
                shape_problems=["turns: the whole table is missing"],
                needs_column_drop=True,
            ),
            ingest_mod.PLAN_REFUSE_SHAPE,
        )
        self.assertEqual(probe.calls, 0)

    def test_the_in_place_upgrade_is_decided_before_the_reaped_source_probe(
        self,
    ) -> None:
        # The ordering the 12-line comment used to carry alone. Asking the
        # guard here would refuse a change that deletes nothing, telling
        # exactly the users whose database is the only copy of their history
        # to stay on the old version.
        probe = self._Probe(True)
        self.assertEqual(
            self._plan(probe, version=6, needs_column_drop=True),
            ingest_mod.PLAN_IN_PLACE,
        )
        self.assertEqual(probe.calls, 0, "the guard must not gate a lossless hop")

    def test_an_unlisted_version_meets_the_guard_before_the_legacy_repair(
        self,
    ) -> None:
        # Matching COLUMNS is not the same fact as a row-preserving DELTA: an
        # unlisted version is precisely one whose row semantics nobody has
        # re-decided, so it does not get to jump the guard.
        probe = self._Probe(True)
        self.assertEqual(
            self._plan(probe, version=4, pending_additions=True),
            ingest_mod.PLAN_REFUSE_REAPED,
        )
        self.assertEqual(probe.calls, 1)

        clear = self._Probe(False)
        self.assertEqual(
            self._plan(clear, version=4, pending_additions=True),
            ingest_mod.PLAN_LEGACY_REPAIR,
        )
        self.assertEqual(clear.calls, 1)

    def test_an_unlisted_version_with_nothing_repairable_rebuilds(self) -> None:
        probe = self._Probe(False)
        self.assertEqual(self._plan(probe, version=4), ingest_mod.PLAN_REBUILD)

    def test_an_old_version_with_an_old_SHAPE_is_rebuilt_not_refused(self) -> None:
        # The shape probe must not swallow the exit it was added in front of.
        # For a version nobody listed, "the tables differ from current" is not
        # damage -- it is what an old version MEANS, and the rebuild is the
        # exit written for it. Refusing here would strand every genuinely old
        # database, including the v0 one the reaped-source guard was built
        # against.
        probe = self._Probe(False)
        self.assertEqual(
            self._plan(
                probe,
                version=1,
                shape_problems=["api_calls: missing column(s) message_id"],
            ),
            ingest_mod.PLAN_REBUILD,
        )
        # ...and it still meets the guard on the way, which is what makes
        # rebuilding it safe rather than merely permitted.
        reaped = self._Probe(True)
        self.assertEqual(
            self._plan(
                reaped,
                version=1,
                shape_problems=["api_calls: missing column(s) message_id"],
            ),
            ingest_mod.PLAN_REFUSE_REAPED,
        )

    def test_an_unverified_shape_never_takes_the_legacy_repair(self) -> None:
        # The pre-v5 repair stamps too, so #35 applies to it identically: it
        # may only run over a shape that has been checked. The fall-through is
        # a rebuild rather than a refusal because this exit is PAST the guard,
        # which has already established that every source can be re-read.
        probe = self._Probe(False)
        self.assertEqual(
            self._plan(
                probe,
                version=4,
                pending_additions=True,
                shape_problems=["turns: the whole table is missing"],
            ),
            ingest_mod.PLAN_REBUILD,
        )

    def test_an_old_sqlite_with_a_column_to_drop_declines_the_shortcuts(
        self,
    ) -> None:
        # Without DROP COLUMN the in-place route cannot REACH the current
        # shape, and stamping most of it is the #35 defect by another door.
        probe = self._Probe(False)
        self.assertEqual(
            self._plan(
                probe, version=7, needs_column_drop=True, can_drop_column=False
            ),
            ingest_mod.PLAN_REBUILD,
        )
        self.assertEqual(
            self._plan(
                probe,
                version=4,
                pending_additions=True,
                needs_column_drop=True,
                can_drop_column=False,
            ),
            ingest_mod.PLAN_REBUILD,
        )

    def test_capability_is_only_consulted_when_there_is_something_to_drop(
        self,
    ) -> None:
        # #42(a): the gate asked whether this SQLite CAN drop a column, not
        # whether anything needs dropping, so a database that already lacks
        # `cost_usd` was pushed onto the rebuild path -- and refused there
        # forever on a reaped corpus -- for work that was a no-op.
        probe = self._Probe(True)
        self.assertEqual(
            self._plan(
                probe, version=7, needs_column_drop=False, can_drop_column=False
            ),
            ingest_mod.PLAN_IN_PLACE,
        )
        self.assertEqual(probe.calls, 0)

    def test_a_current_database_needing_no_work_is_left_alone(self) -> None:
        probe = self._Probe(False)
        self.assertEqual(self._plan(probe), ingest_mod.PLAN_CURRENT)
        # ...but one the unprobed path stamped can still be missing a column
        # it can supply, and at the current version nothing else ever will.
        self.assertEqual(
            self._plan(probe, pending_additions=True), ingest_mod.PLAN_IN_PLACE
        )
        self.assertEqual(probe.calls, 0)


class InPlaceRepairSetsAreDecidedTest(unittest.TestCase):
    """What an in-place upgrade may CREATE and may ADD, named and pinned.

    Both sets are permissions to write something a transcript did not: a table
    created from nothing, or a column with no measured value in it. Each entry
    has to be true of the DATA, not just of the SQL -- so they are listed here
    with the reason, and a new entry has to change this test.
    """

    def test_only_a_stamp_and_a_census_may_be_created_from_nothing(self) -> None:
        # `ingest_runs` holds no row derived from a transcript, so creating it
        # empty states the truth ("no run recorded yet"), which is exactly what
        # a v6 database's state is. `turns` created empty is a lie about a
        # source that `ingest_state` still marks unchanged.
        #
        # `source_shape` (#15) qualifies for the same reason and only because
        # a row in it is a POSITIVE observation: empty means "nothing censused
        # yet", which is a v8 database's true state. It is admitted WITH the
        # cost -- `ingest()` reports censused-of-tracked, so an empty census is
        # never read as a clean corpus (`ShapeCensusUpgradeTest`).
        self.assertEqual(
            ingest_mod.IN_PLACE_CREATABLE_TABLES,
            frozenset({ingest_mod.INGEST_RUNS_TABLE, ingest_mod.SHAPE_TABLE}),
        )

    def test_only_these_columns_may_be_added_to_rows_that_already_exist(
        self,
    ) -> None:
        # `archived_at`: NULL means "still on disk", which is true of every row
        # written before the column existed, and the next run recomputes it from
        # the filesystem.
        #
        # The three cache-miss columns (#5): NULL means "this row was written
        # before CPB read `message.diagnostics`", i.e. UNMEASURED -- true of
        # every v10 row by construction, and distinct from every value the
        # reader can write (`CacheMissDiagnosticsUpgradeTest`). A NOT NULL
        # column, or one whose NULL would be read as a measurement, cannot be
        # added this way at all.
        #
        # The two per-TTL cache-write columns (#84): NULL means "this row was
        # written before CPB read `usage.cache_creation`" -- again unmeasured,
        # and again true of every pre-v12 row by construction. Both plausible
        # back-fills are FALSE rather than merely imprecise, which is what makes
        # NULL the only admissible value: 0 would state the call wrote no cache
        # at that TTL, and copying `cache_write` into `cache_write_5m` would
        # assert a 1.25x write for the 26.86% of cache-write tokens measured as
        # 1-hour 2x writes (`PerTtlCacheWriteUpgradeTest`).
        self.assertEqual(
            dict(ingest_mod.IN_PLACE_ADDABLE_COLUMNS),
            {
                ("ingest_state", "archived_at"): "REAL",
                ("api_calls", "cache_miss_outcome"): "TEXT",
                ("api_calls", "cache_miss_reason"): "TEXT",
                ("api_calls", "cache_missed_input_tokens"): "INTEGER",
                ("api_calls", "cache_write_5m"): "INTEGER",
                ("api_calls", "cache_write_1h"): "INTEGER",
            },
        )

    def test_every_addable_column_is_in_the_shipped_schema(self) -> None:
        # A repair that adds a column the schema does not declare would leave
        # the database permanently unable to pass its own shape probe.
        shape = ingest_mod._target_shape()
        for (table, column) in ingest_mod.IN_PLACE_ADDABLE_COLUMNS:
            with self.subTest(table=table, column=column):
                self.assertIn(column, shape[table])

    def test_every_creatable_table_is_in_the_shipped_schema(self) -> None:
        for table in ingest_mod.IN_PLACE_CREATABLE_TABLES:
            with self.subTest(table=table):
                self.assertIn(table, ingest_mod.DERIVED_TABLES)


class CapabilityGateAsksAboutNeedTest(unittest.TestCase):
    """A database with nothing to drop must not be refused for want of DROP
    COLUMN (#42(a)).

    The gate read `_sqlite_supports_drop_column()`, which is a fact about the
    LIBRARY. What the in-place path needs is a fact about the DATABASE: whether
    `api_calls` still carries `cost_usd`. A v6/v7-stamped database that already
    lacks it -- reachable by crashing mid-upgrade on a new SQLite, or by moving
    a file between machines -- failed a gate about work it did not need, fell
    through to the rebuild path, and was refused there forever if any source
    had been reaped. Measured before the fix, 2026-08-05:

        before: version=7 rows=5 cost_usd=False
        ->  SystemExit: REFUSING to rebuild the database.
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cpb-need-gate-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.projects = self.tmp / "projects"
        self.projects.mkdir()
        self.transcript = self.projects / "session-fixture.jsonl"
        shutil.copy(FIXTURE, self.transcript)
        self.db = self.tmp / "usage.db"
        ingest(self.projects, self.db)

    def _rows(self) -> int:
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute("SELECT COUNT(*) FROM api_calls").fetchone()[0]
        finally:
            conn.close()

    def _state(self) -> tuple[int, bool]:
        conn = sqlite3.connect(self.db)
        try:
            return (
                conn.execute("PRAGMA user_version").fetchone()[0],
                "cost_usd" in ingest_mod._table_columns(conn, "api_calls"),
            )
        finally:
            conn.close()

    def test_a_v7_shape_with_nothing_to_drop_upgrades_on_an_old_sqlite(
        self,
    ) -> None:
        self.transcript.unlink()
        ingest(self.projects, self.db)  # archives it; rows retained
        conn = sqlite3.connect(self.db)
        conn.execute("PRAGMA user_version = 7")
        conn.commit()
        conn.close()
        before = self._rows()
        self.assertEqual(self._state(), (7, False))

        with mock.patch.object(sqlite3, "sqlite_version", "3.34.1"):
            summary = ingest(self.projects, self.db)  # must not raise SystemExit

        self.assertFalse(summary["schema_rebuilt"])
        self.assertEqual(summary["files_ingested"], 0)
        self.assertEqual(self._rows(), before)
        self.assertEqual(self._state(), (ingest_mod.SCHEMA_VERSION, False))

    def test_a_pre_v5_shape_with_nothing_to_drop_repairs_on_an_old_sqlite(
        self,
    ) -> None:
        # The same gate, second instance: the legacy `archived_at` repair asked
        # the same capability question about the same absent work.
        conn = sqlite3.connect(self.db)
        self.addCleanup(conn.close)
        conn.execute(
            "CREATE TABLE ingest_state_old (path TEXT PRIMARY KEY,"
            " session_id TEXT NOT NULL, source_kind TEXT NOT NULL,"
            " size INTEGER NOT NULL, mtime REAL NOT NULL,"
            " unparsed_records INTEGER NOT NULL, first_ts REAL, last_ts REAL)"
        )
        conn.execute(
            "INSERT INTO ingest_state_old SELECT path, session_id, source_kind,"
            " size, mtime, unparsed_records, first_ts, last_ts FROM ingest_state"
        )
        conn.execute("DROP TABLE ingest_state")
        conn.execute("ALTER TABLE ingest_state_old RENAME TO ingest_state")
        conn.execute("PRAGMA user_version = 4")
        conn.commit()
        before = self._rows()

        with mock.patch.object(sqlite3, "sqlite_version", "3.34.1"):
            rebuilt = ingest_mod._prepare_schema(conn)

        self.assertFalse(rebuilt, "nothing needed dropping, so nothing needed a rebuild")
        self.assertIn("archived_at", ingest_mod._table_columns(conn, "ingest_state"))
        self.assertEqual(self._rows(), before)
        self.assertEqual(
            conn.execute("PRAGMA user_version").fetchone()[0],
            ingest_mod.SCHEMA_VERSION,
        )


class RetiredColumnDropRefusalTest(unittest.TestCase):
    """A DROP COLUMN that SQLite structurally refuses must say so, and must NOT
    fall back to a rebuild (#42(b)).

    SQLite refuses to drop a column an index, view or trigger references. No
    CPB schema has ever indexed `cost_usd`, so reaching this needs a user who
    queried their own database directly -- which the durability design
    positively encourages.

    The obvious fallback is a trap. Rebuilding here meets the reaped-source
    guard, which refuses precisely when the database is the only surviving copy
    of its measurements, so on a corpus older than the retention window the
    sequence becomes *drop refused -> rebuild refused*: two individually
    correct refusals composing into no upgrade path at all. So the drop refuses
    with an ACTION instead -- naming the object to remove, which is a one-line,
    non-destructive step the operator can take, after which the in-place hop
    proceeds.
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cpb-drop-refusal-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.projects = self.tmp / "projects"
        self.projects.mkdir()
        self.transcript = self.projects / "session-fixture.jsonl"
        shutil.copy(FIXTURE, self.transcript)
        self.db = self.tmp / "usage.db"
        ingest(self.projects, self.db)
        conn = sqlite3.connect(self.db)
        try:
            conn.execute("ALTER TABLE api_calls ADD COLUMN cost_usd REAL")
            conn.execute("UPDATE api_calls SET cost_usd = id * 0.25 + 0.125")
            conn.execute("PRAGMA user_version = 7")
            conn.commit()
        finally:
            conn.close()

    def _snapshot(self) -> list[tuple]:
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute(
                "SELECT id, session_id, model, output_tokens, cost_usd"
                " FROM api_calls ORDER BY id"
            ).fetchall()
        finally:
            conn.close()

    def test_an_index_over_the_retired_column_produces_a_named_refusal(
        self,
    ) -> None:
        conn = sqlite3.connect(self.db)
        try:
            conn.execute("CREATE INDEX idx_my_own_cost ON api_calls (cost_usd)")
            conn.commit()
        finally:
            conn.close()
        before = self._snapshot()
        self.assertTrue(before, "the fixture must hold rows for this to mean anything")

        with self.assertRaises(SystemExit) as caught:
            ingest(self.projects, self.db)

        message = str(caught.exception)
        self.assertIn("REFUSING", message)
        # The operator cannot act on "something references it".
        self.assertIn("idx_my_own_cost", message)
        self.assertIn("cost_usd", message)

    def test_the_refusal_changes_nothing_and_does_not_reach_a_rebuild(
        self,
    ) -> None:
        conn = sqlite3.connect(self.db)
        try:
            conn.execute("CREATE INDEX idx_my_own_cost ON api_calls (cost_usd)")
            conn.commit()
        finally:
            conn.close()
        before = self._snapshot()

        with self.assertRaises(SystemExit):
            ingest(self.projects, self.db)

        conn = sqlite3.connect(self.db)
        try:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 7)
            self.assertIn("cost_usd", ingest_mod._table_columns(conn, "api_calls"))
        finally:
            conn.close()
        self.assertEqual(self._snapshot(), before)

    def test_removing_the_index_leaves_the_upgrade_available(self) -> None:
        # The property that makes a refusal legitimate rather than a dead end:
        # the operator has a next step, and taking it works.
        conn = sqlite3.connect(self.db)
        try:
            conn.execute("CREATE INDEX idx_my_own_cost ON api_calls (cost_usd)")
            conn.commit()
        finally:
            conn.close()
        with self.assertRaises(SystemExit):
            ingest(self.projects, self.db)

        conn = sqlite3.connect(self.db)
        try:
            conn.execute("DROP INDEX idx_my_own_cost")
            conn.commit()
        finally:
            conn.close()

        summary = ingest(self.projects, self.db)

        self.assertFalse(summary["schema_rebuilt"])
        self.assertEqual(summary["files_ingested"], 0)
        conn = sqlite3.connect(self.db)
        try:
            self.assertNotIn("cost_usd", ingest_mod._table_columns(conn, "api_calls"))
            self.assertEqual(
                conn.execute("PRAGMA user_version").fetchone()[0],
                ingest_mod.SCHEMA_VERSION,
            )
        finally:
            conn.close()


class SchemaCommentPlacementTest(unittest.TestCase):
    """No `--` comment may live inside a `CREATE TABLE` body (#42(b)).

    `ALTER TABLE ... DROP COLUMN` makes SQLite reconstruct the table's stored
    CREATE statement. On SQLite 3.45.1 that reconstruction fails when the
    dropped column is LAST and the text directly above it is a `--` comment:
    the closing paren is re-appended at the truncation point, which lands
    inside the commented-out line, so the statement never closes.

        sqlite3.OperationalError: error in table ingest_state after drop
        column: incomplete input

    Measured 2026-08-05: red on all four CI legs (Python 3.10-3.13, SQLite
    3.45.1), green on a 3.53.3 laptop, which discards the comment block
    entirely. The mechanism is inferred from the error string; the version
    boundary is measured.

    CPB's schema is unusually comment-heavy and three tables ended in a
    commented column, so the next migration that drops a last column would
    have passed locally and failed CI -- a failure that arrives after review,
    on a machine nobody can attach a debugger to.

    This is enforced as an AUTHORING RULE rather than a runtime fallback on
    purpose. The fallback the issue first proposed -- catch the error and
    rebuild -- collides with the reaped-source guard, which refuses a rebuild
    exactly when the database is the only copy of its measurements: two correct
    refusals composing into no upgrade path. A comment that is not in the
    stored statement cannot break its reconstruction on any version.
    """

    def _stored_statements(self) -> dict[str, str]:
        conn = sqlite3.connect(":memory:")
        try:
            conn.executescript(ingest_mod.SCHEMA)
            return {
                name: sql
                for name, sql in conn.execute(
                    "SELECT name, sql FROM sqlite_master WHERE type='table'"
                    " AND name NOT LIKE 'sqlite_%'"
                )
            }
        finally:
            conn.close()

    def test_no_stored_create_statement_carries_a_comment(self) -> None:
        # Asserted over what SQLITE STORES, not over the text of SCHEMA:
        # comments ABOVE a CREATE TABLE are not part of the statement and are
        # free, which is where these ones now live.
        for table, sql in sorted(self._stored_statements().items()):
            with self.subTest(table=table):
                self.assertNotIn("--", sql)

    def test_the_last_column_of_every_table_survives_a_drop(self) -> None:
        # The property the rule exists to protect, exercised directly. It has
        # no teeth on a 3.53.3 laptop and full teeth on CI's 3.45.1, which is
        # the asymmetry that produced the finding in the first place.
        #
        # Tables whose last column is a primary key or is named by an index are
        # skipped: SQLite refuses those drops for a reason that is not the
        # reconstruction defect, and forcing them would test its refusal
        # instead of this rule.
        for table in ingest_mod.DERIVED_TABLES:
            conn = sqlite3.connect(":memory:")
            try:
                conn.executescript(ingest_mod.SCHEMA)
                *_, name, _type, _notnull, _default, pk = conn.execute(
                    f"PRAGMA table_info({table})"
                ).fetchall()[-1]
                if pk:
                    continue
                indexed = False
                for index in conn.execute(f"PRAGMA index_list({table})").fetchall():
                    columns = conn.execute(f"PRAGMA index_info({index[1]})")
                    indexed = indexed or name in {row[2] for row in columns}
                if indexed:
                    continue
                with self.subTest(table=table, column=name):
                    conn.execute(f"ALTER TABLE {table} DROP COLUMN {name}")
                    # The reconstruction is lazy on some builds; force SQLite
                    # to re-read the statement it just wrote.
                    conn.execute(f"SELECT COUNT(*) FROM {table}")
            finally:
                conn.close()


# ---------------------------------------------------------------------------
# Transcript-shape census fixtures (#15).
#
# Anthropic documents the JSONL entry format as INTERNAL and changing between
# Claude Code versions, so every assumption CPB makes about it has to be
# COUNTED rather than assumed. These fixtures pin the counts deliberately
# unequal -- per file and in the corpus total -- so a census that mixed two
# versions up, or that counted files instead of records, cannot pass.
#
#   file      version A   version B   no version field
#   one           3           -              1
#   two           2           6              0
#   three         -           -              7
#   TOTAL         5           6              8
#
# Version strings are synthetic and unlike any real one; the real corpus is
# never quoted here (CLAUDE.md: never commit captured session content).
SHAPE_SESSION = "shape-census"
SHAPE_VERSION_A = "7.3.101"
SHAPE_VERSION_B = "7.3.202"
# Unequal per class AND unequal to every other fixture in this file, so a
# swapped column mapping cannot pass through the shape tests either.
SHAPE_USAGE = {
    "input_tokens": 31,
    "cache_creation_input_tokens": 37,
    "cache_read_input_tokens": 41,
    "output_tokens": 43,
}


def shape_line(
    record_type: str,
    *,
    version: object = None,
    usage: object = None,
    message_id: str = None,
    omit_type: bool = False,
) -> str:
    """One JSONL record, built from parts, for the shape-census fixtures."""
    record: dict = {
        "sessionId": SHAPE_SESSION,
        "timestamp": "2026-08-05T10:00:00.000Z",
    }
    if not omit_type:
        record["type"] = record_type
    if version is not None:
        record["version"] = version
    if record_type == "assistant":
        message: dict = {
            "model": "claude-sonnet-5-20260115",
            "usage": dict(SHAPE_USAGE) if usage is None else usage,
        }
        if message_id is not None:
            message["id"] = message_id
        record["message"] = message
    elif record_type == "user":
        record["message"] = {"role": "user", "content": "a turn"}
    return json.dumps(record) + "\n"


def census_rows(
    db: Path, fact: str, path: Path = None
) -> dict:
    """`{name: records}` for one shape fact, corpus-wide or for one source.

    Returned as a plain dict rather than a Counter on purpose: a Counter
    answers 0 for a name it has never seen, which is exactly the
    absence-rendered-as-a-value the census exists to prevent, and it would
    make every "this name was never recorded" assertion below vacuous.

    The per-source branch REFUSES to fold two rows with the same name into
    one. It did fold them at first, and that made a real defect invisible:
    with the delete-before-insert removed from `store_source`, a re-ingested
    file accumulated a second set of rows and this helper quietly kept the
    last of each -- a fixture making the defect undetectable, which is the one
    thing CLAUDE.md says a fixture may never do. Verified by mutation.
    """
    conn = sqlite3.connect(db)
    try:
        if path is None:
            # GROUP BY, so one row per name by construction.
            return {
                name: records
                for name, records in conn.execute(
                    f"SELECT name, SUM(records) FROM {ingest_mod.SHAPE_TABLE}"
                    " WHERE fact = ? GROUP BY name",
                    (fact,),
                ).fetchall()
            }
        rows = conn.execute(
            f"SELECT name, records FROM {ingest_mod.SHAPE_TABLE}"
            " WHERE fact = ? AND path = ?",
            (fact, str(path)),
        ).fetchall()
    finally:
        conn.close()
    census: dict = {}
    for name, records in rows:
        if name in census:
            raise AssertionError(
                f"two {fact} rows name the same thing for one source"
                f" ({name!r}: {census[name]} and {records}). The census must be"
                " REPLACED per file, not accumulated across parses."
            )
        census[name] = records
    return census


class TranscriptVersionCensusTest(unittest.TestCase):
    """The Claude Code `version` that wrote each measured record, counted (#15).

    Anthropic's own documentation says the entry format "changes between
    versions", so the version is the single most load-bearing piece of
    provenance a transcript carries -- and CPB stored none of it. A composition
    figure spanning two incompatible versions could not even be identified as
    one.

    The census ranges over the records CPB DERIVES NUMBERS FROM (assistant and
    user), not over every line: an aggregate must name the set it ranges over,
    and 10 of the 14 record types on the reference corpus never carry a version
    at all, so counting them would bury the one case that matters -- a MEASURED
    record with no version, of which there are 0 in 524,160 (checked
    2026-08-05).
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cpb-version-census-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.projects = self.tmp / "projects"
        self.projects.mkdir()
        self.db = self.tmp / "usage.db"

        self.one = self.projects / "census-one.jsonl"
        self.one.write_text(
            shape_line("assistant", version=SHAPE_VERSION_A, message_id="m-one-1")
            + shape_line("user", version=SHAPE_VERSION_A)
            + shape_line("assistant", version=SHAPE_VERSION_A, message_id="m-one-2")
            + shape_line("user")
        )
        # A session that spanned a Claude Code upgrade: TWO versions in ONE
        # file, which is why the census cannot be a single column per source.
        self.two = self.projects / "census-two.jsonl"
        self.two.write_text(
            "".join(
                shape_line(
                    "assistant", version=SHAPE_VERSION_B, message_id=f"m-two-{i}"
                )
                for i in range(6)
            )
            + shape_line("user", version=SHAPE_VERSION_A)
            + shape_line("assistant", version=SHAPE_VERSION_A, message_id="m-two-a")
        )
        self.three = self.projects / "census-three.jsonl"
        self.three.write_text(
            "".join(
                shape_line("assistant", message_id=f"m-three-{i}") for i in range(4)
            )
            + "".join(shape_line("user") for _ in range(3))
        )
        self.summary = ingest(self.projects, self.db)

    def test_each_version_is_counted_per_source_file(self) -> None:
        self.assertEqual(
            census_rows(self.db, ingest_mod.SHAPE_VERSION, self.one),
            {SHAPE_VERSION_A: 3, None: 1},
        )
        self.assertEqual(
            census_rows(self.db, ingest_mod.SHAPE_VERSION, self.two),
            {SHAPE_VERSION_B: 6, SHAPE_VERSION_A: 2, None: 0},
        )

    def test_two_sources_reporting_different_versions_stay_distinguishable(
        self,
    ) -> None:
        # The whole point of keying the census on the SOURCE: a corpus-wide
        # "versions seen" list cannot say which file to distrust.
        conn = sqlite3.connect(self.db)
        try:
            rows = conn.execute(
                f"SELECT path, name FROM {ingest_mod.SHAPE_TABLE}"
                " WHERE fact = ? AND name IS NOT NULL",
                (ingest_mod.SHAPE_VERSION,),
            ).fetchall()
        finally:
            conn.close()
        by_path: dict = {}
        for path, name in rows:
            by_path.setdefault(path, set()).add(name)
        self.assertEqual(by_path[str(self.one)], {SHAPE_VERSION_A})
        self.assertEqual(by_path[str(self.two)], {SHAPE_VERSION_A, SHAPE_VERSION_B})
        self.assertNotIn(str(self.three), by_path)

    def test_a_source_reporting_no_version_names_none(self) -> None:
        # Rule #12: absent is not a version. The file gets a counted ABSENCE,
        # never a substituted string and never a row naming some other file's
        # version.
        self.assertEqual(
            census_rows(self.db, ingest_mod.SHAPE_VERSION, self.three), {None: 7}
        )

    def test_a_file_whose_records_all_report_a_version_records_a_measured_zero(
        self,
    ) -> None:
        # 0 is a healthy sample here and must stay distinguishable from "this
        # source was never censused" (which is NO row at all -- see
        # ShapeCensusUpgradeTest).
        self.assertEqual(
            census_rows(self.db, ingest_mod.SHAPE_VERSION, self.two)[None], 0
        )

    def test_the_corpus_census_sums_records_not_files(self) -> None:
        self.assertEqual(
            census_rows(self.db, ingest_mod.SHAPE_VERSION),
            {SHAPE_VERSION_A: 5, SHAPE_VERSION_B: 6, None: 8},
        )

    def test_an_empty_version_string_is_an_absence_not_a_version(self) -> None:
        custom = self.projects / "empty-version.jsonl"
        custom.write_text(shape_line("assistant", version="", message_id="m-empty"))
        parsed = parse_file(custom)
        self.assertEqual(
            parsed.shape[(ingest_mod.SHAPE_VERSION, None)],
            1,
            "an empty version string must count as an absence",
        )
        self.assertNotIn((ingest_mod.SHAPE_VERSION, ""), parsed.shape)

    def test_a_line_that_is_not_json_is_not_censused(self) -> None:
        # It is already counted as unparsed; censusing it would invent a
        # versionless MEASURED record out of a line we could not read at all.
        custom = self.projects / "broken.jsonl"
        custom.write_text(
            shape_line("assistant", version=SHAPE_VERSION_A, message_id="m-ok")
            + "{not json at all\n"
        )
        parsed = parse_file(custom)
        self.assertEqual(parsed.unparsed_records, 1)
        self.assertEqual(parsed.shape[(ingest_mod.SHAPE_VERSION, SHAPE_VERSION_A)], 1)
        self.assertEqual(parsed.shape[(ingest_mod.SHAPE_VERSION, None)], 0)

    def test_the_run_summary_reports_how_much_of_the_corpus_is_censused(
        self,
    ) -> None:
        # "no unknown shapes" and "nothing has been looked at" are different
        # facts and the summary has to tell them apart.
        self.assertEqual(self.summary["sources_censused"], 3)
        self.assertEqual(self.summary["sources_tracked"], 3)

    def test_pruning_a_source_removes_its_census(self) -> None:
        self.three.unlink()
        ingest(
            self.projects, self.db,
            prune_missing=True, approve_prune=approve_this_prune,
        )
        self.assertEqual(census_rows(self.db, ingest_mod.SHAPE_VERSION, self.three), {})
        self.assertEqual(
            census_rows(self.db, ingest_mod.SHAPE_VERSION),
            {SHAPE_VERSION_A: 5, SHAPE_VERSION_B: 6, None: 1},
        )

    def test_re_ingesting_a_changed_file_replaces_its_census(self) -> None:
        # The census is per FILE and must not accumulate across re-parses --
        # doubling it would misreport which version wrote a corpus.
        with open(self.one, "a") as fh:
            fh.write(
                shape_line("assistant", version=SHAPE_VERSION_B, message_id="m-new")
            )
        ingest(self.projects, self.db)
        self.assertEqual(
            census_rows(self.db, ingest_mod.SHAPE_VERSION, self.one),
            {SHAPE_VERSION_A: 3, SHAPE_VERSION_B: 1, None: 1},
        )


class UnknownRecordTypeCensusTest(unittest.TestCase):
    """A record type this tool has never heard of is COUNTED and NAMED (#15).

    The ingest skipped every non-assistant/user record without counting it, on
    the comment "known-irrelevant". That is true of the 12 types measured on
    the reference corpus and says nothing at all about the 13th: if a Claude
    Code release renamed `assistant`, every total would drop quietly and no
    counter in this tool would move.

    Reference corpus, checked 2026-08-05: 638,813 records over 2,952 files and
    18 projects, carrying exactly the 14 types in `KNOWN_RECORD_TYPES`.
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cpb-type-census-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.projects = self.tmp / "projects"
        self.projects.mkdir()
        self.db = self.tmp / "usage.db"
        self.source = self.projects / "unknown-types.jsonl"
        # Counts deliberately unequal: 2 of one unknown type, 1 untyped
        # record, 3 of a KNOWN-irrelevant type, 1 measured assistant record.
        self.source.write_text(
            shape_line("sparkle-event")
            + shape_line("sparkle-event")
            + shape_line("assistant", omit_type=True, message_id="m-untyped")
            + "".join(shape_line("mode") for _ in range(3))
            + shape_line("assistant", version=SHAPE_VERSION_A, message_id="m-known")
        )
        self.summary = ingest(self.projects, self.db)

    def test_an_unknown_type_is_counted_under_its_own_name(self) -> None:
        self.assertEqual(
            census_rows(self.db, ingest_mod.SHAPE_UNKNOWN_RECORD_TYPE, self.source),
            {"sparkle-event": 2, None: 1},
        )

    def test_a_known_irrelevant_type_is_not_reported_as_unknown(self) -> None:
        # Otherwise the loud count is loud on every run and stops being read.
        self.assertNotIn(
            "mode",
            census_rows(self.db, ingest_mod.SHAPE_UNKNOWN_RECORD_TYPE, self.source),
        )

    def test_an_unknown_type_does_not_become_a_parse_failure(self) -> None:
        # Two different facts: `unparsed_records` says a record we TRIED to
        # read defeated us; the census says we did not try. Folding one into
        # the other would make the existing INCONCLUSIVE figure mean two
        # things.
        self.assertEqual(self.summary["unparsed_records"], 0)

    def test_an_unknown_type_does_not_zero_out_what_was_measured(self) -> None:
        # "Counting an unknown record type must not turn it into a known zero
        # for some other type": the one real assistant record is still a call.
        conn = sqlite3.connect(self.db)
        try:
            row = conn.execute(
                "SELECT COUNT(*), SUM(output_tokens) FROM api_calls"
            ).fetchone()
        finally:
            conn.close()
        # ONE call: the record whose `type` is absent carries a `message.usage`
        # and is still NOT measured, because nothing says what it is. It is
        # counted as an unknown shape instead -- unmeasured, not zero.
        self.assertEqual(row[0], 1)
        self.assertEqual(row[1], SHAPE_USAGE["output_tokens"])

    def test_the_summary_names_the_unknown_types(self) -> None:
        # A count with no name is unactionable -- the operator cannot look up
        # a type they cannot see.
        self.assertEqual(
            self.summary["unknown_record_types"], {"sparkle-event": 2, None: 1}
        )

    def test_the_census_survives_a_run_that_skips_every_file(self) -> None:
        # The loud signal must not be a one-shot: the file is unchanged on the
        # next run and therefore skipped, and the shape finding still stands.
        again = ingest(self.projects, self.db)
        self.assertEqual(again["files_ingested"], 0)
        self.assertEqual(
            again["unknown_record_types"], {"sparkle-event": 2, None: 1}
        )


class UsageShapeCensusTest(unittest.TestCase):
    """An unexpected `usage` shape is counted and named, never silently read
    past (#15).

    Two directions, and the second is the dangerous one:

      * a key this tool has never seen (`output_tokens_details` is documented
        and present in API responses, and appears on 0 of 338,030 assistant
        records in the reference corpus, checked 2026-08-05) -- a signal that
        the token accounting may have moved;
      * one of the four keys CPB's numbers are made of going MISSING. `tok()`
        reads an absent key as a real 0, which is right for a token class that
        did not occur and catastrophic if a release renames `output_tokens`:
        every figure would read zero and nothing would say so. All four are
        present on 338,030 of 338,030 assistant records.
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cpb-usage-census-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.projects = self.tmp / "projects"
        self.projects.mkdir()
        self.db = self.tmp / "usage.db"

    def _usage_with(self, **overrides) -> dict:
        usage = dict(SHAPE_USAGE)
        usage.update(overrides)
        return usage

    def test_an_unseen_usage_key_is_counted_under_its_own_name(self) -> None:
        source = self.projects / "unknown-usage-keys.jsonl"
        # Deliberately unequal: 2 records carry one new key, 1 carries another.
        source.write_text(
            "".join(
                shape_line(
                    "assistant",
                    version=SHAPE_VERSION_A,
                    message_id=f"m-details-{i}",
                    usage=self._usage_with(output_tokens_details={"reasoning": 5}),
                )
                for i in range(2)
            )
            + shape_line(
                "assistant",
                version=SHAPE_VERSION_A,
                message_id="m-sparkle",
                usage=self._usage_with(sparkle_tokens=9),
            )
        )
        ingest(self.projects, self.db)
        self.assertEqual(
            census_rows(self.db, ingest_mod.SHAPE_UNKNOWN_USAGE_KEY, source),
            {"output_tokens_details": 2, "sparkle_tokens": 1},
        )

    def test_an_unseen_usage_key_does_not_stop_the_call_being_measured(
        self,
    ) -> None:
        source = self.projects / "extra-key.jsonl"
        source.write_text(
            shape_line(
                "assistant",
                version=SHAPE_VERSION_A,
                message_id="m-extra",
                usage=self._usage_with(sparkle_tokens=9),
            )
        )
        parsed = parse_file(source)
        self.assertEqual(parsed.unparsed_records, 0)
        self.assertEqual(len(parsed.calls), 1)
        self.assertEqual(parsed.calls[0].output_tokens, SHAPE_USAGE["output_tokens"])

    def test_a_usage_of_exactly_the_expected_shape_names_nothing(self) -> None:
        source = self.projects / "clean-usage.jsonl"
        source.write_text(
            shape_line("assistant", version=SHAPE_VERSION_A, message_id="m-clean")
        )
        ingest(self.projects, self.db)
        self.assertEqual(
            census_rows(self.db, ingest_mod.SHAPE_UNKNOWN_USAGE_KEY, source), {}
        )
        self.assertEqual(
            census_rows(self.db, ingest_mod.SHAPE_MISSING_USAGE_KEY, source), {}
        )

    def test_every_key_the_token_columns_are_read_from_is_counted_when_absent(
        self,
    ) -> None:
        # Parameterised over the constant itself, so a key added to the tuple
        # without a census is a failing test rather than a silent zero.
        for key in ingest_mod.USAGE_TOKEN_KEYS:
            with self.subTest(missing=key):
                usage = dict(SHAPE_USAGE)
                del usage[key]
                source = self.projects / f"missing-{key}.jsonl"
                source.write_text(
                    shape_line(
                        "assistant",
                        version=SHAPE_VERSION_A,
                        message_id=f"m-{key}",
                        usage=usage,
                    )
                )
                parsed = parse_file(source)
                self.assertEqual(
                    parsed.shape[(ingest_mod.SHAPE_MISSING_USAGE_KEY, key)], 1
                )
                # ...and the record is still stored, with the honest 0 that
                # absence has always meant. The census is the thing that keeps
                # that 0 distinguishable from a renamed key.
                self.assertEqual(len(parsed.calls), 1)
                self.assertEqual(parsed.unparsed_records, 0)

    def test_a_malformed_value_is_still_unparsed_and_still_censused(self) -> None:
        # The two mechanisms are independent: a record can defeat the parser
        # AND tell us which version wrote it. Losing the second because of the
        # first is how a breaking release stays anonymous.
        source = self.projects / "malformed-and-versioned.jsonl"
        source.write_text(
            shape_line(
                "assistant",
                version=SHAPE_VERSION_B,
                message_id="m-bad",
                usage=self._usage_with(input_tokens="oops", sparkle_tokens=1),
            )
        )
        parsed = parse_file(source)
        self.assertEqual(parsed.unparsed_records, 1)
        self.assertEqual(len(parsed.calls), 0)
        self.assertEqual(parsed.shape[(ingest_mod.SHAPE_VERSION, SHAPE_VERSION_B)], 1)
        self.assertEqual(
            parsed.shape[(ingest_mod.SHAPE_UNKNOWN_USAGE_KEY, "sparkle_tokens")], 1
        )


class ShapeCensusUpgradeTest(unittest.TestCase):
    """v8 -> v9 adds the census table and must not cost a single row (#15).

    The census is the whole reason the schema moved, and a rebuild to install
    it would meet the reaped-source guard -- refusing exactly the corpora whose
    database is the only surviving copy. So `source_shape` is added to
    `IN_PLACE_CREATABLE_TABLES`, which is a claim that an EMPTY one is TRUE of
    a v8 database. It is: a row in it is a POSITIVE observation about a source,
    so no rows means "nothing has been censused yet", which is precisely a v8
    database's state. That is why "this source has no census" is a queryable
    fact rather than something a reader has to infer from silence.
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cpb-census-upgrade-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.projects = self.tmp / "projects"
        self.projects.mkdir()
        self.transcript = self.projects / "session-fixture.jsonl"
        shutil.copy(FIXTURE, self.transcript)
        self.db = self.tmp / "usage.db"
        ingest(self.projects, self.db)

    def _downgrade_to_v8(self) -> None:
        """The v8 shape: everything current, minus the census table."""
        conn = sqlite3.connect(self.db)
        try:
            conn.execute(f"DROP TABLE {ingest_mod.SHAPE_TABLE}")
            conn.execute("PRAGMA user_version = 8")
            conn.commit()
        finally:
            conn.close()

    def _counts(self) -> tuple:
        conn = sqlite3.connect(self.db)
        try:
            return (
                conn.execute("SELECT COUNT(*) FROM api_calls").fetchone()[0],
                conn.execute("SELECT COUNT(*) FROM ingest_state").fetchone()[0],
            )
        finally:
            conn.close()

    def test_a_v8_database_upgrades_in_place_without_re_parsing(self) -> None:
        before = self._counts()
        self._downgrade_to_v8()
        summary = ingest(self.projects, self.db)
        self.assertFalse(summary["schema_rebuilt"])
        self.assertEqual(summary["files_ingested"], 0)
        self.assertEqual(summary["files_skipped"], summary["files_scanned"])
        self.assertEqual(self._counts(), before)
        conn = sqlite3.connect(self.db)
        try:
            self.assertEqual(
                conn.execute("PRAGMA user_version").fetchone()[0],
                ingest_mod.SCHEMA_VERSION,
            )
        finally:
            conn.close()

    def test_the_upgrade_is_not_refused_over_a_reaped_source(self) -> None:
        self.transcript.unlink()
        ingest(self.projects, self.db)  # archives it; rows retained
        before = self._counts()
        self._downgrade_to_v8()
        ingest(self.projects, self.db)  # must not raise SystemExit
        self.assertEqual(self._counts(), before)

    def test_an_uncensused_source_is_not_reported_as_a_clean_one(self) -> None:
        # The cost of creating the table empty, paid honestly: a v8 database's
        # sources have NOT been censused, and the run summary must say so
        # rather than let an empty census read as "no unknown shapes found".
        self._downgrade_to_v8()
        summary = ingest(self.projects, self.db)
        self.assertEqual(summary["sources_tracked"], 1)
        self.assertEqual(summary["sources_censused"], 0)
        self.assertEqual(census_rows(self.db, ingest_mod.SHAPE_VERSION), {})

    def test_a_censused_source_and_an_uncensused_one_are_distinguishable(
        self,
    ) -> None:
        self._downgrade_to_v8()
        ingest(self.projects, self.db)
        fresh = self.projects / "second.jsonl"
        fresh.write_text(
            shape_line("assistant", version=SHAPE_VERSION_A, message_id="m-fresh")
        )
        summary = ingest(self.projects, self.db)
        self.assertEqual(summary["sources_tracked"], 2)
        self.assertEqual(summary["sources_censused"], 1)
        # No row for the old source; a named row for the new one. "Never
        # looked" and "looked, found no version" are different shapes of
        # answer, and the second one is a row with a NULL name.
        self.assertEqual(
            census_rows(self.db, ingest_mod.SHAPE_VERSION, self.transcript), {}
        )
        self.assertEqual(
            census_rows(self.db, ingest_mod.SHAPE_VERSION, fresh),
            {SHAPE_VERSION_A: 1, None: 0},
        )


class SingleFileShapeCensusTest(unittest.TestCase):
    """Single-file mode censuses the ONE file it opened, and claims no more.

    The mode's whole discipline is that it looked at one file, so it may
    conclude nothing about any other source. The census obeys the same rule
    from both directions: the summary reports THIS file's shape rather than the
    database's roll-up (which directory mode reports), and on the SKIP path it
    carries no census keys at all -- an empty census would state that a file
    was examined and found unsurprising, when it was never opened.

    Storage is shared with directory mode through `store_source()`, which is
    what keeps the two modes from describing the same file differently.
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cpb-single-census-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.projects = self.tmp / "projects"
        self.projects.mkdir()
        self.db = self.tmp / "usage.db"
        self.one = self.projects / "single-census.jsonl"
        self.one.write_text(
            shape_line("assistant", version=SHAPE_VERSION_A, message_id="m-single")
            + shape_line("user", version=SHAPE_VERSION_A)
            + shape_line("sparkle-event")
        )
        # A second source the run must say nothing about.
        self.other = self.projects / "untouched.jsonl"
        self.other.write_text(
            shape_line("assistant", version=SHAPE_VERSION_B, message_id="m-other")
        )

    def test_the_censused_file_is_the_one_that_was_opened(self) -> None:
        summary = ingest_mod.ingest_transcript(self.one, self.db)
        self.assertEqual(summary["files_ingested"], 1)
        self.assertEqual(summary["claude_code_versions"], {SHAPE_VERSION_A: 2, None: 0})
        self.assertEqual(summary["unknown_record_types"], {"sparkle-event": 1})
        self.assertEqual(
            census_rows(self.db, ingest_mod.SHAPE_VERSION, self.one),
            {SHAPE_VERSION_A: 2, None: 0},
        )
        # The other file was never opened, so it has no census -- not an empty
        # one, and certainly not this file's.
        self.assertEqual(
            census_rows(self.db, ingest_mod.SHAPE_VERSION, self.other), {}
        )

    def test_the_skip_path_reports_no_census_at_all(self) -> None:
        ingest_mod.ingest_transcript(self.one, self.db)
        again = ingest_mod.ingest_transcript(self.one, self.db)
        self.assertEqual(again["files_skipped"], 1)
        # Absent keys, not empty ones: nothing was parsed, so there is nothing
        # to report, and reporting {} would say the file was found clean.
        for key in (
            "claude_code_versions",
            "unknown_record_types",
            "unknown_usage_keys",
            "missing_usage_keys",
        ):
            with self.subTest(key=key):
                self.assertNotIn(key, again)
        # ...and the stored census from the first run is untouched.
        self.assertEqual(
            census_rows(self.db, ingest_mod.SHAPE_VERSION, self.one),
            {SHAPE_VERSION_A: 2, None: 0},
        )

    def test_the_summary_never_carries_a_corpus_wide_coverage_claim(self) -> None:
        summary = ingest_mod.ingest_transcript(self.one, self.db)
        # Directory mode reports censused-of-tracked. This mode cannot: it has
        # not looked at the other sources, so it does not count them.
        self.assertNotIn("sources_tracked", summary)
        self.assertNotIn("sources_censused", summary)


class RealTranscriptShapeSmokeTest(unittest.TestCase):
    """The assumed shape, checked against a REAL transcript (#15).

    Every other test in this file runs on a synthetic fixture that this
    repository wrote, so all of them agree with CPB's assumptions by
    construction. They cannot notice the one failure mode Anthropic documents:
    "the entry format is internal to Claude Code and changes between versions".
    This class is the only one that can, because it reads what Claude Code
    actually wrote on this machine.

    **The privacy tension, and how it is resolved.** Real transcripts contain
    prompts, file paths and source code, and CLAUDE.md forbids committing any
    of it -- so this test may not carry a captured fixture, may not write one,
    and may not quote what it read. It therefore reads the corpus AT TEST TIME
    from `~/.claude/projects` and asserts only over the SHAPE CENSUS: key
    names, record-type names and counts, all of which are vocabulary of the
    format rather than content of a session. No assertion message can contain
    a prompt, a path or a session id, so a failure is safe to paste into CI.

    The tradeoff is that this test cannot run where there is no corpus --
    CI included. It skips there, LOUDLY: the skip reason is printed to stderr
    as well as recorded, because a shape test that silently passes on the
    machine that runs the merge is worth nothing at all. What it buys is that
    the break surfaces on a developer's machine the first time they run the
    suite after a Claude Code upgrade, which is months before a chart would
    have quietly changed shape.
    """

    # Bounded: the reference corpus is 2,952 files and 638,813 records and
    # takes ~26 s to walk. The most recently written transcripts are the ones
    # a new Claude Code release will have touched, so they are the sample.
    MAX_FILES = 3

    NO_CORPUS = (
        "SKIPPED LOUDLY: no Claude Code transcripts under ~/.claude/projects, "
        "so the assumed transcript SHAPE was NOT checked against anything real "
        "on this machine. Every other test in this suite runs on fixtures this "
        "repository wrote, which agree with CPB's assumptions by construction "
        "(#15)."
    )

    @classmethod
    def setUpClass(cls) -> None:
        """Find and parse the sample ONCE, and say so when there is none.

        Parsed here rather than per test method so the four assertions below
        read the SAME sample -- a live transcript grows between two reads of
        it, and four disagreeing samples would make a failure unreproducible.
        The banner is printed here for the same reason: once per run, to
        stderr, so the skip is visible without `-v` and cannot pass for green.
        """
        transcripts = []
        for project in available_projects():
            transcripts.extend(project.glob(f"*{ingest_mod.TRANSCRIPT_SUFFIX}"))
        readable = []
        for path in transcripts:
            try:
                readable.append((path.stat().st_mtime, path))
            except OSError:
                continue
        readable.sort(reverse=True)
        cls.sample = [path for _mtime, path in readable[: cls.MAX_FILES]]
        cls.parsed = [parse_file(path) for path in cls.sample]
        if not cls.parsed:
            print(f"\n{cls.NO_CORPUS}", file=sys.stderr)

    def setUp(self) -> None:
        # Skipped per TEST, not per class, so every one of these assertions is
        # individually reported as "not checked here" rather than the class
        # quietly vanishing from the run.
        if not self.parsed:
            self.skipTest(self.NO_CORPUS)

    def _census(self, fact: str) -> dict:
        totals: dict = {}
        for parsed in self.parsed:
            for (kind, name), records in parsed.shape.items():
                if kind == fact:
                    totals[name] = totals.get(name, 0) + records
        return totals

    def test_every_record_type_is_one_this_tool_has_heard_of(self) -> None:
        unknown = self._census(ingest_mod.SHAPE_UNKNOWN_RECORD_TYPE)
        self.assertEqual(
            unknown,
            {},
            "a real transcript carries record type(s) this tool does not know."
            " Names and counts above; nothing else from the file is shown."
            " Decide what each one means, then add it to KNOWN_RECORD_TYPES"
            " (or teach the parser to read it) -- a type nobody has looked at"
            " is spend that may be leaving the totals silently.",
        )

    def test_usage_carries_exactly_the_keys_this_tool_expects(self) -> None:
        unknown = self._census(ingest_mod.SHAPE_UNKNOWN_USAGE_KEY)
        self.assertEqual(
            unknown,
            {},
            "a real `usage` object carries key(s) this tool has never seen."
            " Check what they measure before adding them to KNOWN_USAGE_KEYS:"
            " a new key can mean the token accounting itself has moved"
            " (`output_tokens_details` is the documented candidate).",
        )
        missing = self._census(ingest_mod.SHAPE_MISSING_USAGE_KEY)
        self.assertEqual(
            missing,
            {},
            "a real `usage` object is MISSING a key every CPB token column is"
            " read from. An absent key is read as a real 0, so this is the"
            " shape change that would zero out the report without raising"
            " anything at all.",
        )

    def test_the_measured_records_still_report_a_claude_code_version(self) -> None:
        versions = self._census(ingest_mod.SHAPE_VERSION)
        named = {name for name in versions if name is not None}
        self.assertTrue(
            named,
            "no record in the sampled real transcripts carries a `version`"
            " field. The per-source version census cannot be derived from"
            " anything else, so it is now blank rather than wrong -- but the"
            " field has moved or gone, which is exactly the instability this"
            " test exists to surface.",
        )

    def test_the_parser_still_finds_api_calls_in_a_real_transcript(self) -> None:
        calls = sum(len(parsed.calls) for parsed in self.parsed)
        self.assertGreater(
            calls,
            0,
            "no `message.usage` record was found in the most recently written"
            " real transcripts. Either the sampled sessions genuinely made no"
            " API call, or the assistant/usage shape has moved -- and the"
            " second one silently empties every figure this tool reports.",
        )
        # A call whose context is 0 across the board would satisfy the count
        # above while measuring nothing: the tokens have to be there too.
        self.assertGreater(
            sum(call.context_size for parsed in self.parsed for call in parsed.calls),
            0,
            "every real API call measured a context of 0 tokens.",
        )

    def test_real_records_still_carry_the_cache_miss_diagnostics_field(self) -> None:
        # #5 reads Anthropic's own classification off `message.diagnostics`
        # instead of re-deriving it, which is only possible while Claude Code
        # keeps persisting it -- a documented-as-internal field, so this is
        # exactly the #15 exposure. Measured 2026-08-05 on this machine: present
        # on 341,489 of 341,597 assistant records, the 108 exceptions being the
        # `<synthetic>` local-error placeholders.
        #
        # Asserted over OUTCOMES, never over reasons: whether a sampled session
        # happened to miss the cache is chance, but whether the field was there
        # at all is the format.
        outcomes = [
            call.cache_miss_outcome
            for parsed in self.parsed
            for call in parsed.calls
        ]
        self.assertTrue(outcomes, "no calls in the sample")
        self.assertTrue(
            any(o != ingest_mod.CACHE_DIAG_ABSENT for o in outcomes),
            "no record in the sampled real transcripts carries a `diagnostics`"
            " field at all. Either Claude Code stopped sending the"
            " cache-diagnosis beta header or the field moved -- and CPB's"
            " cache-miss reporting then reads as a corpus that never missed the"
            " cache, rather than as one nobody measured.",
        )

    def test_no_real_record_carries_a_cache_miss_type_this_tool_cannot_place(
        self,
    ) -> None:
        # A seventh member of the taxonomy is stored by name and placed in
        # NEITHER family, so it can never be counted as a cause. This is where
        # it becomes visible instead of merely harmless.
        unplaced = {
            call.cache_miss_reason
            for parsed in self.parsed
            for call in parsed.calls
            if call.cache_miss_outcome
            in (ingest_mod.CACHE_DIAG_UNRECOGNISED, ingest_mod.CACHE_DIAG_UNREADABLE)
        }
        self.assertEqual(
            unplaced,
            set(),
            "a real transcript carries a `cache_miss_reason` this tool cannot"
            " place (name above, or None where the shape itself defeated the"
            " read). Decide which family it belongs to -- divergence, or no"
            " comparison produced -- before anything counts it.",
        )


# ---------------------------------------------------------------------------
# Duplicate-dispatch fixtures (#36)
# ---------------------------------------------------------------------------
#
# Every value below is deliberately UNEQUAL to every other, and the two rows of
# each colliding pair are deliberately ASYMMETRIC: they carry different
# metadata, so a per-field merge, a wrong "richest" rule and a plain "last one
# wins" all produce different rows and only one of them is the row these tests
# assert. A fixture whose duplicate rows were identical would pass under every
# one of those rules, which is the fixture-makes-the-defect-undetectable
# failure CLAUDE.md forbids.

DISPATCH_USAGE = {
    "input_tokens": 11,
    "cache_creation_input_tokens": 22,
    "cache_read_input_tokens": 33,
    "output_tokens": 44,
}


def dispatch_lines(
    session: str,
    seq: int,
    *,
    task_id: str = None,
    agent_type: str = None,
    description: str = None,
    subagent_tokens: int = None,
    tool_use_id: str = None,
) -> str:
    """One dispatch, in the two records a transcript actually records it as.

    An `Agent` tool_use carries the agent type and description; the
    `<task-notification>` turn that closes it carries the task id and the
    self-reported `<subagent_tokens>` figure. They are SEPARATE records on
    purpose -- a resumed session's transcript can hold the notification without
    the tool_use that opened it, which is precisely how the two rows of a real
    collision come to differ in richness (measured 2026-08-05: 15 of the 24
    duplicate rows on the reference corpus carry no `agent_type` at all).

    `tool_use_id` reuses an earlier dispatch's tool_use rather than emitting a
    new one, which is how a repeat notification about ONE task is written.
    """
    lines = ""
    if tool_use_id is None and (agent_type is not None or description is not None):
        tool_use_id = f"toolu-{session}-{seq}"
        lines += json.dumps(
            {
                "type": "assistant",
                "sessionId": session,
                "timestamp": f"2026-08-05T12:{seq:02d}:00.000Z",
                "message": {
                    "id": f"msg-{session}-{seq}",
                    "model": "claude-sonnet-5-20260115",
                    "usage": dict(DISPATCH_USAGE),
                    "content": [
                        {
                            "type": "tool_use",
                            "id": tool_use_id,
                            "name": "Agent",
                            "input": {
                                "subagent_type": agent_type,
                                "description": description,
                                "prompt": "do the thing",
                            },
                        }
                    ],
                },
            }
        ) + "\n"
    text = "<task-notification>\n"
    if task_id is not None:
        text += f"<task-id>{task_id}</task-id>\n"
    if tool_use_id is not None:
        text += f"<tool-use-id>{tool_use_id}</tool-use-id>\n"
    text += "<status>completed</status>\n"
    if subagent_tokens is not None:
        text += (
            f"<usage><subagent_tokens>{subagent_tokens}</subagent_tokens></usage>\n"
        )
    text += "</task-notification>"
    return lines + json.dumps(
        {
            "type": "user",
            "sessionId": session,
            "timestamp": f"2026-08-05T12:{seq:02d}:30.000Z",
            "message": {"role": "user", "content": [{"type": "text", "text": text}]},
        }
    ) + "\n"


# The resume pair the reference corpus is made of: ONE dispatch recorded in TWO
# session transcripts. `resume-a` sorts first, so it is ingested first and is
# the incumbent for every collision below.
RESUME_A = (
    # RICHER side of a collision: named agent and description, but the
    # transcript never captured a <subagent_tokens> figure.
    dispatch_lines(
        "resume-a", 1,
        task_id="task-shared",
        agent_type="backend-expert",
        description="Rebuild the ledger",
    )
    # POORER side of the eviction collision: only the self-reported figure.
    + dispatch_lines("resume-a", 2, task_id="task-evict", subagent_tokens=6553)
    # A dispatch only this transcript records: the single-row control.
    + dispatch_lines(
        "resume-a", 3,
        task_id="task-solo-a",
        agent_type="frontend-expert",
        description="Restyle the panel",
        subagent_tokens=4211,
    )
    # No <task-id> at all. NULL is not a shared key: this row and the one in
    # resume-b are two dispatches, never one.
    + dispatch_lines(
        "resume-a", 4,
        agent_type="reviewer",
        description="Look again",
        subagent_tokens=1307,
    )
    # SAME-SESSION repeat: two notifications about one task in one file, the
    # second carrying the finished figure. Handled before the database sees it.
    + dispatch_lines(
        "resume-a", 5,
        task_id="task-twice",
        agent_type="docs-expert",
        description="Write it down",
        subagent_tokens=100,
    )
    + dispatch_lines(
        "resume-a", 6,
        task_id="task-twice",
        tool_use_id="toolu-resume-a-5",
        subagent_tokens=2200,
    )
    # EQUALLY rich side of a tie, and the one stored first. Its counterpart in
    # resume-b knows exactly as much and says something different, so the
    # tie-break is the only thing that can decide between them.
    + dispatch_lines(
        "resume-a", 7,
        task_id="task-tie",
        agent_type="tie-expert",
        description="Weighs the same",
    )
)

RESUME_B = (
    # POORER side of `task-shared`: no tool_use survived into this transcript,
    # so no agent type and no description -- but it DID capture a figure the
    # richer row lacks, which is the genuinely ambiguous case.
    dispatch_lines("resume-b", 1, task_id="task-shared", subagent_tokens=9973)
    # RICHER side of `task-evict`, arriving second: it must EVICT the row
    # already stored, or "richest wins" is really "first wins".
    + dispatch_lines(
        "resume-b", 2,
        task_id="task-evict",
        agent_type="wizard",
        description="Finish the ledger",
        subagent_tokens=6553,
    )
    + dispatch_lines(
        "resume-b", 3,
        agent_type="reviewer-two",
        description="Look once more",
        subagent_tokens=8081,
    )
    # The other side of the tie: same richness, different values. A tie keeps
    # the row already stored, so this one must LOSE -- and "richest wins" read
    # as ">=" would keep it instead.
    + dispatch_lines(
        "resume-b", 4,
        task_id="task-tie",
        agent_type="tie-imposter",
        description="Also weighs the same",
    )
)


class DuplicateDispatchTaskIdTest(unittest.TestCase):
    """One dispatch recorded in two transcripts is ONE row (#36).

    `agent_dispatches` had no uniqueness on `task_id`, and the session panel
    joins measured spend to it, so where two rows shared a `task_id` the same
    agent's spend was counted twice. Measured 2026-08-05 on a reference corpus:
    1,865 dispatch rows, 1,853 distinct `task_id`s, 12 duplicated ids over 24
    rows, ALL of them across two sessions and none within one -- resume chains,
    where a session continues under a new id and the same dispatch is recorded
    again. `calls_for_agent` is identical for both rows of a pair because the
    `task_id` IS the agent, which is what makes it one dispatch rather than two.

    The rules these tests pin, in the shape `_dedupe_calls()` established for
    `message.id`:

      * ONE WHOLE ROW survives. Never a per-field merge -- a synthesised row
        describes a dispatch that no transcript recorded.
      * RICHEST wins, and richness is the count of non-NULL metadata fields.
      * A tie keeps the row already stored, so a re-ingest does not churn.
      * NULL `task_id` is NOT a shared key.
      * The collision is COUNTED, and so is the case where the discarded row
        carried something the survivor does not.
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cpb-dispatch-dedupe-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.projects = self.tmp / "projects"
        self.projects.mkdir()
        self.a = self.projects / "resume-a.jsonl"
        self.a.write_text(RESUME_A)
        self.b = self.projects / "resume-b.jsonl"
        self.b.write_text(RESUME_B)
        self.db = self.tmp / "usage.db"
        self.summary = ingest(self.projects, self.db)

    def _rows(self) -> dict:
        conn = sqlite3.connect(self.db)
        try:
            return {
                task_id: (session_id, agent_type, description, subagent_tokens)
                for task_id, session_id, agent_type, description, subagent_tokens
                in conn.execute(
                    "SELECT task_id, session_id, agent_type, description,"
                    " subagent_tokens FROM agent_dispatches"
                    " WHERE task_id IS NOT NULL"
                )
            }
        finally:
            conn.close()

    def test_each_task_id_is_stored_exactly_once(self) -> None:
        conn = sqlite3.connect(self.db)
        self.addCleanup(conn.close)
        rows, distinct = conn.execute(
            "SELECT COUNT(*), COUNT(DISTINCT task_id) FROM agent_dispatches"
            " WHERE task_id IS NOT NULL"
        ).fetchone()
        self.assertEqual(distinct, 5)  # shared, evict, solo-a, twice, tie
        self.assertEqual(rows, distinct)

    def test_the_richest_whole_row_survives_never_a_field_merge(self) -> None:
        # The anti-merge assertion. A per-field maximum would report
        # backend-expert / Rebuild the ledger / 9973 -- a dispatch neither
        # transcript recorded. `subagent_tokens` MUST still be NULL here.
        self.assertEqual(
            self._rows()["task-shared"],
            ("resume-a", "backend-expert", "Rebuild the ledger", None),
        )

    def test_a_richer_row_evicts_one_already_stored(self) -> None:
        # Ingested second and kept, so the rule is richness and not arrival
        # order. The evicted row's figure agreed, so nothing was lost with it.
        self.assertEqual(
            self._rows()["task-evict"],
            ("resume-b", "wizard", "Finish the ledger", 6553),
        )

    def test_a_tie_keeps_the_row_already_stored(self) -> None:
        # Both rows know exactly as much, so richness cannot decide and the
        # tie-break has to. Keeping the incumbent is what makes re-ingesting
        # the other transcript a no-op rather than a rewrite.
        self.assertEqual(
            self._rows()["task-tie"],
            ("resume-a", "tie-expert", "Weighs the same", None),
        )

    def test_a_dispatch_only_one_transcript_records_is_untouched(self) -> None:
        self.assertEqual(
            self._rows()["task-solo-a"],
            ("resume-a", "frontend-expert", "Restyle the panel", 4211),
        )

    def test_a_repeat_notification_in_one_file_keeps_the_finished_figure(self) -> None:
        # Two notifications about ONE task in ONE transcript are two
        # notifications, not two recordings of one -- the later carries the
        # finished figure, so the in-file rule stays "last wins" and the
        # cross-file rule is the one that asks about richness. The row never
        # reaches the database twice, so no collision is counted for it.
        self.assertEqual(
            self._rows()["task-twice"],
            ("resume-a", "docs-expert", "Write it down", 2200),
        )

    def test_null_task_ids_are_two_dispatches_and_never_merged(self) -> None:
        # Guarded TWICE, which is why this one needs a two-line mutation to go
        # red: `_resolve_dispatch` returns before it looks anything up, and the
        # lookup itself uses `= ?`, which matches no row when the value is
        # NULL. Removing either alone leaves the behaviour correct; removing
        # both merges these two rows and this assertion fails (verified).
        conn = sqlite3.connect(self.db)
        self.addCleanup(conn.close)
        rows = conn.execute(
            "SELECT session_id, subagent_tokens FROM agent_dispatches"
            " WHERE task_id IS NULL ORDER BY session_id"
        ).fetchall()
        self.assertEqual(rows, [("resume-a", 1307), ("resume-b", 8081)])

    def test_the_collisions_are_counted_and_the_lossy_one_named_apart(self) -> None:
        # Three collisions; TWO of them discarded a row carrying something the
        # survivor does not have (`task-shared`'s figure, `task-tie`'s names).
        # `task-evict`'s discarded row agreed on everything it knew, so nothing
        # went with it. A silent dedupe is indistinguishable from no
        # duplicates, which is the whole reason this pair of counts exists.
        self.assertEqual(self.summary["duplicate_dispatches_resolved"], 3)
        self.assertEqual(self.summary["divergent_dispatch_task_ids"], 2)

    def test_the_run_note_carries_both_counts_and_only_when_there_are_any(
        self,
    ) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            ingest_mod.print_dedupe_note(self.summary)
        note = out.getvalue()
        self.assertIn("duplicate task_id(s) resolved this run: 3", note)
        self.assertIn("survivor does not: 2", note)

        quiet = dict(self.summary)
        quiet["duplicate_dispatches_resolved"] = 0
        quiet["divergent_dispatch_task_ids"] = 0
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            ingest_mod.print_dedupe_note(quiet)
        # Silence where there is nothing to report, so the line means
        # something when it appears -- and never reads as a standing claim
        # that this corpus has no duplicates.
        self.assertNotIn("dispatch dedupe", out.getvalue())

    def test_the_dispatch_count_is_not_silently_reduced(self) -> None:
        # Deduping SPEND must not quietly delete dispatches: the five distinct
        # task ids plus the two unkeyed rows are all still there.
        conn = sqlite3.connect(self.db)
        self.addCleanup(conn.close)
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM agent_dispatches").fetchone()[0], 7
        )

    def test_the_unique_index_is_the_backstop_that_proves_the_dedupe(self) -> None:
        conn = sqlite3.connect(self.db)
        self.addCleanup(conn.close)
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO agent_dispatches (session_id, source_path, task_id)"
                " VALUES ('resume-c', 'nowhere.jsonl', 'task-shared')"
            )
        conn.rollback()
        # ...and it exempts NULL, deliberately and visibly: two more unkeyed
        # rows are two more dispatches.
        conn.execute(
            "INSERT INTO agent_dispatches (session_id, source_path, task_id)"
            " VALUES ('resume-c', 'nowhere.jsonl', NULL)"
        )
        conn.execute(
            "INSERT INTO agent_dispatches (session_id, source_path, task_id)"
            " VALUES ('resume-d', 'nowhere.jsonl', NULL)"
        )
        conn.rollback()

    def test_re_ingesting_a_source_reaches_the_same_rows(self) -> None:
        # The resolution has to CONVERGE: re-parsing the loser's transcript
        # deletes its own rows first, so the collision is re-decided from
        # scratch every time and must land on the same answer.
        before = self._rows()
        os.utime(self.b, (0, 0))
        summary = ingest(self.projects, self.db)
        self.assertEqual(summary["files_ingested"], 1)
        self.assertEqual(self._rows(), before)

    def test_single_file_mode_resolves_and_counts_the_same_collision(self) -> None:
        # Both modes store through `store_source`, which is what keeps them
        # from describing one dispatch differently.
        os.utime(self.b, (0, 0))
        summary = ingest_mod.ingest_transcript(self.b, self.db)
        self.assertEqual(summary["files_ingested"], 1)
        # This file loses `task-shared` and `task-tie` to rows resume-a stored;
        # its own `task-evict` row was deleted with the rest of its source's
        # rows first, so it faces no incumbent and is simply re-inserted.
        self.assertEqual(summary["duplicate_dispatches_resolved"], 2)
        self.assertEqual(summary["divergent_dispatch_task_ids"], 2)
        self.assertEqual(
            self._rows()["task-shared"],
            ("resume-a", "backend-expert", "Rebuild the ledger", None),
        )


class DispatchDedupeMigrationTest(unittest.TestCase):
    """v9 -> v10 resolves the duplicates already stored, IN PLACE (#36).

    A `UNIQUE` index cannot be created over a table that already holds
    duplicates, so the dedupe of existing rows is part of the migration or the
    migration does not run at all. The route matters as much as the result:

      * a table REBUILD would reach the same shape and would be REFUSED by the
        reaped-source guard on exactly the databases that matter most -- the
        ones older than Claude Code's retention window, where the rows are the
        only surviving copy;
      * so the hop is a `CREATE UNIQUE INDEX` preceded by the dedupe, which
        deletes nothing but a second recording of a dispatch another surviving
        row still records.

    Verified on copies of four real databases (v6/v7/v8/v9, 2026-08-05): 1,865
    dispatch rows, 12 duplicated task ids over 24 rows in every one, 0 rows
    with a NULL `task_id`, 7 tracked sources already gone from disk. After the
    upgrade: 1,853 rows, 1,853 distinct task ids, every `api_calls`, `turns`
    and `ingest_state` row untouched.
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cpb-dispatch-migration-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.projects = self.tmp / "projects"
        self.projects.mkdir()
        self.a = self.projects / "resume-a.jsonl"
        self.a.write_text(RESUME_A)
        self.db = self.tmp / "usage.db"
        ingest(self.projects, self.db)
        self._downgrade_to_v9()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db)
        self.addCleanup(conn.close)
        return conn

    def _downgrade_to_v9(self) -> None:
        """The shape v9 shipped: no uniqueness, so a duplicate row can exist.

        Two duplicates, both INSERTED as the second transcript of a resume
        chain recorded them rather than as copies of the rows already there, so
        a migration that kept the wrong row of either cannot pass:

          * `task-shared` -- POORER in metadata, and carrying the one figure
            the stored row lacks. Richness decides it.
          * `task-solo-a` -- EQUALLY rich and saying something different, so
            only the tie-break can decide it. Inserted second, so it holds the
            higher `id` and must lose.
        """
        conn = sqlite3.connect(self.db)
        try:
            conn.execute("DROP INDEX idx_agent_dispatches_task_id")
            conn.executemany(
                "INSERT INTO agent_dispatches (session_id, source_path, turn_id,"
                " task_id, agent_type, description, subagent_tokens)"
                " VALUES ('resume-b', ?, NULL, ?, ?, ?, ?)",
                [
                    (str(self.projects / "resume-b.jsonl"),
                     "task-shared", None, None, 9973),
                    (str(self.projects / "resume-b.jsonl"),
                     "task-solo-a", "frontend-imposter", "Restyle it wrongly", 4211),
                ],
            )
            conn.execute("PRAGMA user_version = 9")
            conn.commit()
        finally:
            conn.close()

    def _state(self) -> tuple:
        conn = sqlite3.connect(self.db)
        try:
            return (
                conn.execute("PRAGMA user_version").fetchone()[0],
                conn.execute("SELECT COUNT(*) FROM api_calls").fetchone()[0],
                conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0],
                conn.execute("SELECT COUNT(*) FROM ingest_state").fetchone()[0],
                conn.execute("SELECT COUNT(*) FROM agent_dispatches").fetchone()[0],
            )
        finally:
            conn.close()

    def _dispatch_rows(self) -> list:
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute(
                "SELECT session_id, task_id, agent_type, description,"
                " subagent_tokens FROM agent_dispatches"
                " ORDER BY task_id IS NULL, task_id, session_id"
            ).fetchall()
        finally:
            conn.close()

    def test_the_fixture_really_is_a_v9_database_with_duplicates(self) -> None:
        version, _calls, _turns, _state, dispatches = self._state()
        self.assertEqual(version, 9)
        self.assertEqual(dispatches, 8)  # 6 parsed + 2 hand-built duplicates
        conn = self._conn()
        self.assertEqual(
            conn.execute(
                "SELECT COUNT(*) FROM agent_dispatches"
                " WHERE task_id IN ('task-shared', 'task-solo-a')"
            ).fetchone()[0],
            4,
        )

    def test_the_upgrade_is_in_place_and_deletes_nothing_else(self) -> None:
        before = self._state()

        rebuilt = ingest_mod._prepare_schema(self._conn())

        self.assertFalse(rebuilt, "a rebuild would meet the reaped-source guard")
        after = self._state()
        self.assertEqual(after[0], ingest_mod.SCHEMA_VERSION)
        # api_calls, turns and ingest_state are untouched; one duplicate
        # dispatch row -- and only that -- is gone.
        self.assertEqual(after[1:4], before[1:4])
        self.assertEqual(after[4], before[4] - 2)

    def test_the_row_the_migration_keeps_is_the_richest_whole_row(self) -> None:
        ingest_mod._prepare_schema(self._conn())
        kept = [
            row for row in self._dispatch_rows()
            if row[1] in ("task-shared", "task-solo-a")
        ]
        # `task-shared` keeps the richer row WITH ITS NULL FIGURE: a per-field
        # merge would have written 9973 beside a name from the other row, a
        # dispatch neither transcript recorded. `task-solo-a` is the tie, and
        # keeps the row stored first.
        self.assertEqual(
            kept,
            [
                ("resume-a", "task-shared", "backend-expert",
                 "Rebuild the ledger", None),
                ("resume-a", "task-solo-a", "frontend-expert",
                 "Restyle the panel", 4211),
            ],
        )

    def test_the_upgrade_is_not_refused_over_a_reaped_source(self) -> None:
        # The case the whole migration design turns on: every transcript gone,
        # so the database is the only copy and a rebuild would be REFUSED.
        self.a.unlink()
        self.assertTrue(ingest_mod._unreadable_sources(self._conn()))
        before = self._state()

        rebuilt = ingest_mod._prepare_schema(self._conn())  # must not raise

        self.assertFalse(rebuilt)
        after = self._state()
        self.assertEqual(after[0], ingest_mod.SCHEMA_VERSION)
        self.assertEqual(after[1:4], before[1:4])
        self.assertEqual(after[4], before[4] - 2)

    def test_an_old_sqlite_with_nothing_to_drop_still_upgrades_in_place(self) -> None:
        # #36 composed with #42(a), which is where a dead end would be built if
        # either gate asked the wrong question. A v9 database has no `cost_usd`
        # to drop, so the DROP COLUMN capability is irrelevant to it -- and this
        # one also has a reaped source, so the rebuild path would REFUSE. Ask
        # about capability rather than need here and the two correct refusals
        # compose into no upgrade path at all.
        self.a.unlink()
        self.assertTrue(ingest_mod._unreadable_sources(self._conn()))
        before = self._state()

        with mock.patch.object(sqlite3, "sqlite_version", "3.34.1"):
            rebuilt = ingest_mod._prepare_schema(self._conn())

        self.assertFalse(rebuilt)
        after = self._state()
        self.assertEqual(after[0], ingest_mod.SCHEMA_VERSION)
        self.assertEqual(after[1:4], before[1:4])
        self.assertEqual(after[4], before[4] - 2)

    def test_the_upgrade_installs_the_constraint_that_proves_it(self) -> None:
        ingest_mod._prepare_schema(self._conn())
        conn = self._conn()
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO agent_dispatches (session_id, source_path, task_id)"
                " VALUES ('resume-c', 'nowhere.jsonl', 'task-shared')"
            )
        conn.rollback()

    def test_the_migration_says_out_loud_what_it_discarded(self) -> None:
        # A dedupe nobody is told about is indistinguishable from no
        # duplicates, and this one deletes rows from a database that may be the
        # only copy of its history.
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            ingest_mod._prepare_schema(self._conn())
        note = out.getvalue()
        self.assertIn("task_id", note)
        # Both counts, because "2 discarded" and "2 of them divergent" are
        # different facts and the operator has to be able to tell them apart.
        self.assertIn("2 duplicate dispatch row(s) discarded", note)
        self.assertIn("does not): 2", note)

    def test_a_database_with_no_duplicates_says_nothing(self) -> None:
        # The other half: no message where there is nothing to report, so the
        # message means something when it appears.
        ingest_mod._prepare_schema(self._conn())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            ingest_mod._prepare_schema(self._conn())
        self.assertEqual(out.getvalue(), "")


# ---------------------------------------------------------------------------
# Cache-miss diagnostics fixtures (#5)
# ---------------------------------------------------------------------------
#
# Claude Code sends the `cache-diagnosis-2026-04-07` beta header and persists
# Anthropic's OWN classification at `message.diagnostics`. CPB reads it; it does
# not re-derive it (a re-derivation is impossible anyway -- 0 assistant records
# on this machine carry a `system` or `tools` field, measured 2026-08-05 over
# 3,012 transcripts; the corpus is live, so treat every count here as a dated
# sample).
#
# Every value below is deliberately UNEQUAL to every other one, in two
# directions at once, because two different defects hide behind sameness:
#
#   * the six `cache_miss_reason.type`s each get their own `input_tokens` and
#     their own `cache_missed_input_tokens`, so a mapping that collapsed two
#     types into one bucket, or wrote the magnitude into the wrong column,
#     cannot pass;
#   * the magnitudes live in a range (1000+) no token value in this fixture
#     reaches, so an estimate landing in a token column is visible as itself.
#
# `MISSED_HUGE` is the no-clamping case: 99,991 against 11 `input_tokens`.
# Anthropic derives the figure from byte lengths BEFORE tokenization and says it
# "can differ from (and occasionally exceed) `usage.input_tokens`" -- measured
# here, it exceeded it on 1,144 of 1,144 records that carried it (2026-08-05),
# so a clamp or a validation would corrupt every single one of them.

_NO_DIAGNOSTICS = object()

MISSED_MODEL = 1031
MISSED_SYSTEM = 1037
MISSED_TOOLS = 1041
MISSED_HUGE = 99991
MISSED_UNKNOWN_TYPE = 1053
MISSED_STREAMED = 1059
MISSED_UNTYPED = 1061

# A seventh type, which the API does not currently define. It stands for the
# release that adds one: CPB must store the name it was given and refuse to
# place it in either family, rather than guess which.
UNKNOWN_MISS_TYPE = "quantum_changed"


def diagnostics_line(
    message_id: str,
    input_tokens: int,
    diagnostics=_NO_DIAGNOSTICS,
    output_tokens: int = 2,
    ts: str = "2026-08-05T12:00:00.000Z",
) -> str:
    """One assistant record, with `message.diagnostics` present or ABSENT.

    The default omits the key ENTIRELY, which is a different fact from
    `"diagnostics": null` and the whole reason this fixture exists.
    """
    message = {
        "id": message_id,
        "model": "claude-sonnet-5-20260115",
        "usage": {
            "input_tokens": input_tokens,
            "cache_creation_input_tokens": input_tokens * 2,
            "cache_read_input_tokens": input_tokens * 3,
            "output_tokens": output_tokens,
        },
        "content": [{"type": "text", "text": "reply"}],
    }
    if diagnostics is not _NO_DIAGNOSTICS:
        message["diagnostics"] = diagnostics
    return json.dumps(
        {
            "type": "assistant",
            "sessionId": "cache-miss-fixture",
            "timestamp": ts,
            "message": message,
        }
    ) + "\n"


def miss_reason(reason_type, missed=None) -> dict:
    """A `diagnostics` object carrying one `cache_miss_reason`."""
    reason = {}
    if reason_type is not _NO_DIAGNOSTICS:
        reason["type"] = reason_type
    if missed is not None:
        reason["cache_missed_input_tokens"] = missed
    return {"cache_miss_reason": reason}


CACHE_MISS_TRANSCRIPT = "".join(
    [
        # 1. no `diagnostics` key at all -- the `<synthetic>` local-error
        #    placeholders are shaped like this (108 of 341,597 records).
        diagnostics_line("msg_absent", 1),
        # 2. `diagnostics: null` -- "first turn, or no divergence found". The
        #    common case by four orders of magnitude (338,692 records).
        diagnostics_line("msg_null", 2, diagnostics=None),
        # 2b. the same statement written the other way: an object that names no
        #     reason. Not observed on this corpus; read identically because it
        #     says the same thing.
        diagnostics_line(
            "msg_reason_null", 37, diagnostics={"cache_miss_reason": None}
        ),
        # 3. the four DIVERGENCES, each carrying a magnitude.
        diagnostics_line(
            "msg_model", 3, diagnostics=miss_reason("model_changed", MISSED_MODEL)
        ),
        diagnostics_line(
            "msg_system", 5, diagnostics=miss_reason("system_changed", MISSED_SYSTEM)
        ),
        diagnostics_line(
            "msg_tools", 7, diagnostics=miss_reason("tools_changed", MISSED_TOOLS)
        ),
        diagnostics_line(
            "msg_messages", 11, diagnostics=miss_reason("messages_changed", MISSED_HUGE)
        ),
        # 4. the two NON-divergences. Neither carries a magnitude on the real
        #    corpus (0 of 1,653 records) and neither is evidence that anything
        #    changed.
        diagnostics_line(
            "msg_prev", 13, diagnostics=miss_reason("previous_message_not_found")
        ),
        diagnostics_line(
            "msg_unavailable", 17, diagnostics=miss_reason("unavailable")
        ),
        # 5. a type from a future release.
        diagnostics_line(
            "msg_unknown_type",
            19,
            diagnostics=miss_reason(UNKNOWN_MISS_TYPE, MISSED_UNKNOWN_TYPE),
        ),
        # 6. shapes the reader cannot trust: `diagnostics` that is not an object,
        #    and a reason object with no `type`. Both must leave the CALL's token
        #    measurement intact -- the usage was fine.
        diagnostics_line("msg_diag_not_an_object", 23, diagnostics=7),
        diagnostics_line(
            "msg_reason_untyped",
            29,
            diagnostics=miss_reason(_NO_DIAGNOSTICS, MISSED_UNTYPED),
        ),
        # 7. a real classification with an unreadable magnitude: the type is
        #    kept (it is independently valid), the magnitude is not invented.
        diagnostics_line(
            "msg_bad_magnitude", 31, diagnostics=miss_reason("tools_changed", "lots")
        ),
        # 8. two STREAMED records of one call (#2): the classification is a
        #    property of the call, so it must survive dedupe exactly once.
        diagnostics_line(
            "msg_streamed",
            41,
            diagnostics=miss_reason("system_changed", MISSED_STREAMED),
            output_tokens=5,
        ),
        diagnostics_line(
            "msg_streamed",
            41,
            diagnostics=miss_reason("system_changed", MISSED_STREAMED),
            output_tokens=9,
        ),
    ]
)


class CacheMissVocabularyTest(unittest.TestCase):
    """The taxonomy has six members and TWO families, not one list (#5).

    `previous_message_not_found` and `unavailable` are not divergences: the
    first means no fingerprint was stored for that id, the second that no
    comparison was produced at all (and is also where a `thinking`, `effort`,
    `tool_choice` or beta-header change lands -- TA-6/TA-7). Counting either as
    a cause of a cache miss invents a cause, which is the exact failure the
    absence rule exists to prevent, committed by the detector written to avoid
    it.

    The names are pinned as LITERALS: they are Anthropic's wire vocabulary, so
    a "tidy-up" rename here would silently stop matching the transcripts.
    """

    def test_all_six_documented_types_are_named(self) -> None:
        self.assertEqual(
            set(ingest_mod.CACHE_MISS_TYPES),
            {
                "model_changed",
                "system_changed",
                "tools_changed",
                "messages_changed",
                "previous_message_not_found",
                "unavailable",
            },
        )

    def test_the_two_families_partition_the_taxonomy(self) -> None:
        divergence = ingest_mod.CACHE_MISS_DIVERGENCE_TYPES
        no_comparison = ingest_mod.CACHE_MISS_NO_COMPARISON_TYPES
        self.assertEqual(divergence | no_comparison, ingest_mod.CACHE_MISS_TYPES)
        self.assertEqual(divergence & no_comparison, frozenset())

    def test_the_absence_states_are_not_divergences(self) -> None:
        for absent in ("previous_message_not_found", "unavailable"):
            with self.subTest(type=absent):
                self.assertIn(absent, ingest_mod.CACHE_MISS_NO_COMPARISON_TYPES)
                self.assertNotIn(absent, ingest_mod.CACHE_MISS_DIVERGENCE_TYPES)

    def test_only_a_changed_thing_is_a_divergence(self) -> None:
        self.assertEqual(
            set(ingest_mod.CACHE_MISS_DIVERGENCE_TYPES),
            {"model_changed", "system_changed", "tools_changed", "messages_changed"},
        )

    def test_the_stored_outcomes_are_named_and_none_of_them_is_a_reason(self) -> None:
        # The outcome column answers "what KIND of sample is this call", the
        # reason column answers "which member of the taxonomy". If a value
        # appeared in both vocabularies a reader grouping by one would be
        # silently reading the other.
        self.assertEqual(
            set(ingest_mod.CACHE_DIAG_OUTCOMES),
            {
                ingest_mod.CACHE_DIAG_ABSENT,
                ingest_mod.CACHE_DIAG_UNREADABLE,
                ingest_mod.CACHE_DIAG_NO_DIVERGENCE,
                ingest_mod.CACHE_DIAG_DIVERGENCE,
                ingest_mod.CACHE_DIAG_NO_COMPARISON,
                ingest_mod.CACHE_DIAG_UNRECOGNISED,
            },
        )
        self.assertEqual(
            set(ingest_mod.CACHE_DIAG_OUTCOMES) & set(ingest_mod.CACHE_MISS_TYPES),
            set(),
        )


class CacheMissDiagnosticsIngestTest(unittest.TestCase):
    """`message.diagnostics`, read onto the call at the `message.id` grain (#5).

    THREE sample states have to stay apart, and one nullable column cannot
    hold them:

      1. no `diagnostics` key at all -> INCONCLUSIVE. Claude Code persisting
         the field is a Claude Code behaviour, not an API guarantee (#15), so
         its absence is "not sampled", never "no divergence".
      2. `diagnostics` present and naming no reason -> a real ZERO-divergence
         sample: first turn, or nothing diverged.
      3. `diagnostics` naming a type -> classified, in one of two families.

    So the schema carries an OUTCOME column (which of those states, and which
    family) beside the REASON column (which member of the taxonomy). A fourth
    state, NULL, belongs to neither of the three: it means this row was written
    before CPB read diagnostics at all -- see `CacheMissDiagnosticsUpgradeTest`.
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cpb-cache-miss-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.projects = self.tmp / "projects"
        self.projects.mkdir()
        self.transcript = self.projects / "cache-miss-fixture.jsonl"
        self.transcript.write_text(CACHE_MISS_TRANSCRIPT)
        self.db = self.tmp / "usage.db"
        self.summary = ingest(self.projects, self.db)
        self.conn = sqlite3.connect(self.db)
        self.addCleanup(self.conn.close)

    def _rows(self) -> dict:
        return {
            row[0]: row[1:]
            for row in self.conn.execute(
                "SELECT message_id, cache_miss_outcome, cache_miss_reason,"
                " cache_missed_input_tokens FROM api_calls"
            )
        }

    def _count(self, where: str, *params) -> int:
        return self.conn.execute(
            f"SELECT COUNT(*) FROM api_calls WHERE {where}", params
        ).fetchone()[0]

    def test_every_record_parsed_and_one_row_survives_per_call(self) -> None:
        # 15 records, 14 calls: the two streamed records of `msg_streamed` are
        # ONE call. Nothing in this fixture is malformed as a RECORD -- an
        # unreadable `diagnostics` is a refusal to classify, not a parse
        # failure, and must never cost the call its token measurement.
        self.assertEqual(self.summary["unparsed_records"], 0)
        self.assertEqual(self.summary["records_parsed"], 15)
        self.assertEqual(self._count("1=1"), 14)

    def test_each_type_lands_on_its_call_with_its_own_magnitude(self) -> None:
        absent = ingest_mod.CACHE_DIAG_ABSENT
        none = ingest_mod.CACHE_DIAG_NO_DIVERGENCE
        div = ingest_mod.CACHE_DIAG_DIVERGENCE
        nocmp = ingest_mod.CACHE_DIAG_NO_COMPARISON
        unrec = ingest_mod.CACHE_DIAG_UNRECOGNISED
        unread = ingest_mod.CACHE_DIAG_UNREADABLE
        self.assertEqual(
            self._rows(),
            {
                "msg_absent": (absent, None, None),
                "msg_null": (none, None, None),
                "msg_reason_null": (none, None, None),
                "msg_model": (div, "model_changed", MISSED_MODEL),
                "msg_system": (div, "system_changed", MISSED_SYSTEM),
                "msg_tools": (div, "tools_changed", MISSED_TOOLS),
                "msg_messages": (div, "messages_changed", MISSED_HUGE),
                "msg_prev": (nocmp, "previous_message_not_found", None),
                "msg_unavailable": (nocmp, "unavailable", None),
                "msg_unknown_type": (unrec, UNKNOWN_MISS_TYPE, MISSED_UNKNOWN_TYPE),
                "msg_diag_not_an_object": (unread, None, None),
                "msg_reason_untyped": (unread, None, None),
                "msg_bad_magnitude": (div, "tools_changed", None),
                "msg_streamed": (div, "system_changed", MISSED_STREAMED),
            },
        )

    def test_no_sample_is_never_stored_as_a_zero_divergence_sample(self) -> None:
        # The rule this whole design exists for. The record with NO
        # `diagnostics` key and the records that carry one naming no reason are
        # DIFFERENT facts, and a reader asking for either gets only that one.
        self.assertEqual(
            self._count("cache_miss_outcome = ?", ingest_mod.CACHE_DIAG_ABSENT), 1
        )
        self.assertEqual(
            self._count("cache_miss_outcome = ?", ingest_mod.CACHE_DIAG_NO_DIVERGENCE),
            2,
        )
        # ...and neither state is reachable by asking for the other.
        self.assertEqual(
            self._count(
                "cache_miss_outcome = ? AND message_id = 'msg_absent'",
                ingest_mod.CACHE_DIAG_NO_DIVERGENCE,
            ),
            0,
        )

    def test_the_two_absence_states_are_not_counted_as_divergences(self) -> None:
        # A reader ranking "why did the cache miss" ranges over the divergence
        # outcome. `unavailable` and `previous_message_not_found` must not be
        # in it -- they are not causes, and 549 + 206 of the 1,219 classified
        # calls on this machine are exactly those two (2026-08-05).
        diverged = {
            row[0]
            for row in self.conn.execute(
                "SELECT cache_miss_reason FROM api_calls WHERE cache_miss_outcome = ?",
                (ingest_mod.CACHE_DIAG_DIVERGENCE,),
            )
        }
        self.assertEqual(
            diverged,
            {"model_changed", "system_changed", "tools_changed", "messages_changed"},
        )
        self.assertEqual(
            {
                row[0]
                for row in self.conn.execute(
                    "SELECT cache_miss_reason FROM api_calls"
                    " WHERE cache_miss_outcome = ?",
                    (ingest_mod.CACHE_DIAG_NO_COMPARISON,),
                )
            },
            {"previous_message_not_found", "unavailable"},
        )

    def test_an_unknown_type_is_stored_by_name_and_placed_in_no_family(self) -> None:
        # The seventh type a release adds. Guessing its family would be
        # inventing the answer; dropping it would hide the release.
        row = self.conn.execute(
            "SELECT cache_miss_outcome, cache_miss_reason FROM api_calls"
            " WHERE message_id = 'msg_unknown_type'"
        ).fetchone()
        self.assertEqual(row, (ingest_mod.CACHE_DIAG_UNRECOGNISED, UNKNOWN_MISS_TYPE))
        self.assertEqual(
            self._count(
                "cache_miss_outcome IN (?, ?) AND cache_miss_reason = ?",
                ingest_mod.CACHE_DIAG_DIVERGENCE,
                ingest_mod.CACHE_DIAG_NO_COMPARISON,
                UNKNOWN_MISS_TYPE,
            ),
            0,
        )

    def test_the_magnitude_is_stored_verbatim_and_never_clamped(self) -> None:
        # Anthropic derives it from byte lengths BEFORE tokenization and says it
        # can exceed `usage.input_tokens`; measured, it exceeded it on 1,144 of
        # 1,144 records carrying it (2026-08-05). Clamping it to the input, or
        # rejecting it for being larger, would corrupt every one of them.
        row = self.conn.execute(
            "SELECT input_tokens, cache_missed_input_tokens FROM api_calls"
            " WHERE message_id = 'msg_messages'"
        ).fetchone()
        self.assertEqual(row, (11, MISSED_HUGE))
        self.assertGreater(row[1], row[0])

    def test_an_unreadable_diagnostics_costs_the_call_nothing_measured(self) -> None:
        # The usage object was fine; only the classification was not. A
        # refusal to classify must not throw away a real token measurement.
        rows = {
            row[0]: row[1:]
            for row in self.conn.execute(
                "SELECT message_id, input_tokens, cache_read, cache_write,"
                " output_tokens FROM api_calls WHERE message_id IN"
                " ('msg_diag_not_an_object', 'msg_reason_untyped')"
            )
        }
        self.assertEqual(
            rows,
            {
                "msg_diag_not_an_object": (23, 69, 46, 2),
                "msg_reason_untyped": (29, 87, 58, 2),
            },
        )

    def test_a_magnitude_that_cannot_be_read_is_not_invented(self) -> None:
        # A valid type beside an unreadable magnitude: the type stands, the
        # magnitude is NULL rather than 0. A 0 there would say "this miss cost
        # nothing", which is the opposite of not knowing.
        self.assertEqual(
            self.conn.execute(
                "SELECT cache_miss_reason, cache_missed_input_tokens FROM api_calls"
                " WHERE message_id = 'msg_bad_magnitude'"
            ).fetchone(),
            ("tools_changed", None),
        )

    def test_the_classification_survives_dedupe_exactly_once(self) -> None:
        # #2: one API call is many transcript records, each repeating the same
        # `message`. The surviving WHOLE record brings its own classification --
        # measured, records of one id disagreed on `diagnostics` in 0 of 169,331
        # cases (2026-08-05), and a per-field synthesis would describe a
        # response that never happened.
        self.assertEqual(self._count("message_id = 'msg_streamed'"), 1)
        self.assertEqual(
            self.conn.execute(
                "SELECT output_tokens, cache_miss_reason FROM api_calls"
                " WHERE message_id = 'msg_streamed'"
            ).fetchone(),
            (9, "system_changed"),
        )

    def test_records_with_no_diagnostics_are_counted_separately(self) -> None:
        # Counted as their own fact, not folded into a bucket: a corpus where
        # Claude Code stopped writing the field would otherwise read as a
        # corpus with no cache misses.
        self.assertEqual(self.summary["calls_without_diagnostics"], 1)
        self.assertEqual(self.summary["calls_with_unreadable_diagnostics"], 3)

    def test_single_file_mode_reports_the_same_two_counts(self) -> None:
        # The hook path measures the same file and must not report a quieter
        # corpus than the directory path does.
        db = self.tmp / "hook.db"
        summary = ingest_mod.ingest_transcript(self.transcript, db)
        self.assertEqual(summary["calls_without_diagnostics"], 1)
        self.assertEqual(summary["calls_with_unreadable_diagnostics"], 3)

    def test_the_counts_are_said_out_loud(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            ingest_mod.print_cache_diagnostics_note(self.summary)
        note = out.getvalue()
        self.assertIn("no `diagnostics` field: 1", note)
        self.assertIn("unreadable: 3", note)

    def test_a_corpus_with_nothing_to_report_says_nothing(self) -> None:
        # So that the note above MEANS something when it appears.
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            ingest_mod.print_cache_diagnostics_note(
                {"calls_without_diagnostics": 0, "calls_with_unreadable_diagnostics": 0}
            )
        self.assertEqual(out.getvalue(), "")


class CacheMissDiagnosticsUpgradeTest(unittest.TestCase):
    """v10 -> v11 adds the three columns without re-parsing anything (#5).

    The rows a v10 database holds were written by a build that never looked at
    `message.diagnostics`. They upgrade to NULL, which is the fourth state:
    UNMEASURED. Upgrading them to "no divergence" would manufacture a
    zero-divergence sample for every call ever ingested -- a whole corpus of
    confident findings nobody measured.

    Why NULL rather than the `absent` outcome: `absent` is a POSITIVE
    observation ("CPB read this record and it carried no `diagnostics`"), and no
    such observation exists for a row written before the read existed. Same
    distinction `source_shape` draws between an absent row and a measured zero
    (#15), and it resolves the same way: those rows are re-read when their
    source file next changes, not back-filled with a guess.
    """

    # The v10 shape of `api_calls`, STATED rather than reached by DROP COLUMN.
    # Same reasoning as `SchemaShapeProbeTest.INGEST_STATE_WITHOUT_ARCHIVED_AT`:
    # the DROP COLUMN statement is itself version-sensitive, and a fixture that
    # differs across CI legs tests the fixture rather than the code.
    API_CALLS_AT_V10 = """
        CREATE TABLE api_calls_v10 (
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
        )
    """
    V10_COLUMNS = (
        "id, session_id, source_path, source_kind, agent_id, turn_id, ts, model,"
        " input_tokens, cache_read, cache_write, output_tokens, context_size,"
        " is_sidechain, message_id"
    )

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cpb-cache-miss-upgrade-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.projects = self.tmp / "projects"
        self.projects.mkdir()
        self.transcript = self.projects / "cache-miss-fixture.jsonl"
        self.transcript.write_text(CACHE_MISS_TRANSCRIPT)
        self.db = self.tmp / "usage.db"
        ingest(self.projects, self.db)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db)
        self.addCleanup(conn.close)
        return conn

    def _snapshot(self) -> list:
        conn = self._conn()
        return conn.execute(
            f"SELECT {self.V10_COLUMNS} FROM api_calls ORDER BY id"
        ).fetchall()

    def _outcomes(self) -> dict:
        conn = self._conn()
        return {
            row[0]: row[1]
            for row in conn.execute(
                "SELECT COALESCE(cache_miss_outcome, '(null)') o, COUNT(*)"
                " FROM api_calls GROUP BY o"
            )
        }

    def _downgrade_to_v10(self) -> None:
        conn = self._conn()
        conn.execute(self.API_CALLS_AT_V10)
        conn.execute(
            f"INSERT INTO api_calls_v10 SELECT {self.V10_COLUMNS} FROM api_calls"
        )
        conn.execute("DROP TABLE api_calls")
        conn.execute("ALTER TABLE api_calls_v10 RENAME TO api_calls")
        conn.execute("PRAGMA user_version = 10")
        conn.commit()
        self.assertNotIn(
            "cache_miss_outcome", ingest_mod._table_columns(conn, "api_calls")
        )

    def test_a_fresh_ingest_records_an_observation_on_every_row(self) -> None:
        # The baseline the upgrade must NOT reproduce: a row CPB actually read
        # never has a NULL outcome, whatever the transcript said.
        self.assertNotIn("(null)", self._outcomes())

    def test_the_upgrade_carries_the_rows_and_leaves_them_unmeasured(self) -> None:
        self._downgrade_to_v10()
        before = self._snapshot()
        self.assertTrue(before, "the fixture must hold rows for this to mean anything")

        summary = ingest(self.projects, self.db)

        self.assertFalse(summary["schema_rebuilt"])
        # A rebuild empties `ingest_state`, so every file would be re-parsed.
        # Skipping them all is the observable proof the rows were carried.
        self.assertEqual(summary["files_ingested"], 0)
        self.assertEqual(summary["files_skipped"], summary["files_scanned"])
        self.assertEqual(self._snapshot(), before)
        conn = self._conn()
        self.assertEqual(
            conn.execute("PRAGMA user_version").fetchone()[0],
            ingest_mod.SCHEMA_VERSION,
        )
        # EVERY carried row reads unmeasured -- not "no divergence", not
        # "absent", not 0 missed tokens.
        self.assertEqual(self._outcomes(), {"(null)": len(before)})
        self.assertEqual(
            conn.execute(
                "SELECT COUNT(*) FROM api_calls WHERE cache_miss_reason IS NOT NULL"
                " OR cache_missed_input_tokens IS NOT NULL"
            ).fetchone()[0],
            0,
        )

    def test_the_upgrade_is_not_refused_over_a_reaped_source(self) -> None:
        # Past Claude Code's retention window the database is the only copy of
        # its measurements, and a rebuild would be REFUSED. Three added nullable
        # columns must not be the change that strands those databases at v10.
        self.transcript.unlink()
        ingest(self.projects, self.db)  # archives it; rows retained
        self._downgrade_to_v10()
        before = self._snapshot()

        ingest(self.projects, self.db)  # must not raise SystemExit

        self.assertEqual(self._snapshot(), before)
        self.assertEqual(self._outcomes(), {"(null)": len(before)})

    def test_re_ingesting_the_source_replaces_unmeasured_with_a_reading(self) -> None:
        # The other half of "not back-filled with a guess": the answer arrives
        # when the file is read again, which is exactly when it can be known.
        self._downgrade_to_v10()
        ingest(self.projects, self.db)
        self.assertEqual(self._outcomes().get("(null)"), 14)

        self.transcript.write_text(CACHE_MISS_TRANSCRIPT)  # changes mtime/size
        os.utime(self.transcript, (time.time() + 2, time.time() + 2))
        ingest(self.projects, self.db)

        self.assertNotIn("(null)", self._outcomes())
        self.assertEqual(
            self._outcomes()[ingest_mod.CACHE_DIAG_DIVERGENCE], 6
        )


# PER-TTL CACHE-WRITE SPLIT FIXTURE (#84).
#
# Every number below is deliberately DISTINCT from every other, and from the
# flat total, because the defect this guards against is a mapping error that a
# fixture of equal values cannot see. Concretely: 5m != 1h, 5m + 1h != every
# other call's flat total, and no TTL value equals `input_tokens`,
# `cache_read_input_tokens` or `output_tokens` on the same record. A test that
# passes with `cache_write_5m` and `cache_write_1h` swapped, or with either
# reading the flat total, is not a test.
#
# Primes, so no value is a multiple or a sum of the others by accident.
SPLIT_5M = 1013
SPLIT_1H = 1019
SPLIT_FLAT = SPLIT_5M + SPLIT_1H  # the RECONCILING case: 2032

# The unreconciled case, measured on 8 of 170,079 deduped calls (2026-08-05)
# and always in this direction -- the flat total 0 while the split is not, so
# the flat total UNDERSTATES. Kept exactly as read, never summed into agreement.
SPLIT_ORPHAN_1H = 1031

# A third TTL, which the API does not currently emit: 0 of 342,850 assistant
# records carried one. It stands for the release that adds one. CPB must
# CENSUS it by name and must not fold it into either stored column.
UNKNOWN_TTL_KEY = "ephemeral_3h_input_tokens"
UNKNOWN_TTL_VALUE = 1039

_NO_CACHE_CREATION = object()


def split_line(
    message_id: str,
    input_tokens: int,
    *,
    flat: int,
    cache_creation=_NO_CACHE_CREATION,
    output_tokens: int = 3,
    ts: str = "2026-08-05T13:00:00.000Z",
) -> str:
    """One assistant record, with `usage.cache_creation` present or ABSENT.

    The default omits the key ENTIRELY, which is the shape a Claude Code
    release that never wrote the split would produce, and a different fact from
    an empty object. `flat` is passed independently of the split on purpose:
    the two are separate readings and the fixture has to be able to state them
    in disagreement.
    """
    usage: dict = {
        "input_tokens": input_tokens,
        "cache_creation_input_tokens": flat,
        "cache_read_input_tokens": input_tokens * 7,
        "output_tokens": output_tokens,
    }
    if cache_creation is not _NO_CACHE_CREATION:
        usage["cache_creation"] = cache_creation
    return json.dumps(
        {
            "type": "assistant",
            "sessionId": "split-fixture",
            "timestamp": ts,
            "message": {
                "id": message_id,
                "model": "claude-sonnet-5-20260115",
                "usage": usage,
                "content": [{"type": "text", "text": "reply"}],
            },
        }
    ) + "\n"


def both_ttls(five: int = SPLIT_5M, hour: int = SPLIT_1H) -> dict:
    return {
        ingest_mod.CACHE_WRITE_5M_KEY: five,
        ingest_mod.CACHE_WRITE_1H_KEY: hour,
    }


SPLIT_TRANSCRIPT = "".join(
    [
        # 1. BOTH keys present and reconciling -- the corpus's overwhelming
        #    majority (170,071 of 170,079 deduped calls, 2026-08-05).
        split_line("msg_both", 5, flat=SPLIT_FLAT, cache_creation=both_ttls()),
        # 2. FLAT TOTAL, NO SPLIT. Not observed on this machine (0 of 342,850
        #    records) but it is what a Claude Code release predating the field
        #    wrote, and the acceptance rule for it is absolute: UNMEASURED for
        #    both TTLs, never 0 and never "assume all 5-minute".
        split_line("msg_flat_only", 7, flat=SPLIT_FLAT),
        # 3. ONE key present, the other not. A real half-measurement: the
        #    present one is READ, the absent one is unmeasured, and the pair
        #    cannot be reconciled against the flat total at all.
        split_line(
            "msg_only_5m",
            11,
            flat=SPLIT_5M,
            cache_creation={ingest_mod.CACHE_WRITE_5M_KEY: SPLIT_5M},
        ),
        split_line(
            "msg_only_1h",
            13,
            flat=SPLIT_1H,
            cache_creation={ingest_mod.CACHE_WRITE_1H_KEY: SPLIT_1H},
        ),
        # 4. A THIRD, unseen TTL key beside the two known ones. The known two
        #    are still read; the third is censused by name and folded into
        #    neither column.
        split_line(
            "msg_third_ttl",
            17,
            flat=SPLIT_FLAT + UNKNOWN_TTL_VALUE,
            cache_creation=dict(both_ttls(), **{UNKNOWN_TTL_KEY: UNKNOWN_TTL_VALUE}),
        ),
        # 5. THE UNRECONCILED SHAPE, as measured: flat 0, split not. Stored as
        #    read, counted, and never made to agree.
        #
        #    STREAMED, deliberately, and the reason is a fixture defect this
        #    caught. Every counted shape below has to arrive on a MULTI-RECORD
        #    call somewhere in this fixture, or moving the three counters back
        #    in front of `_dedupe_calls` changes no number here and the
        #    per-call grain is untested. Verified by mutation: with the
        #    unreconciled and unreadable shapes on single-record calls only,
        #    counting records instead of calls SURVIVED.
        split_line(
            "msg_unreconciled",
            19,
            flat=0,
            cache_creation={
                ingest_mod.CACHE_WRITE_5M_KEY: 0,
                ingest_mod.CACHE_WRITE_1H_KEY: SPLIT_ORPHAN_1H,
            },
        ),
        split_line(
            "msg_unreconciled",
            19,
            flat=0,
            cache_creation={
                ingest_mod.CACHE_WRITE_5M_KEY: 0,
                ingest_mod.CACHE_WRITE_1H_KEY: SPLIT_ORPHAN_1H,
            },
            output_tokens=8,
        ),
        # 6. Shapes the reader cannot trust: `cache_creation` that is not an
        #    object, and a TTL value that is not an integer. Both must leave the
        #    call's four measured token classes intact -- the usage was fine.
        split_line("msg_not_an_object", 23, flat=SPLIT_FLAT, cache_creation=7),
        split_line(
            "msg_bad_value",
            29,
            flat=SPLIT_FLAT,
            cache_creation={
                ingest_mod.CACHE_WRITE_5M_KEY: SPLIT_5M,
                ingest_mod.CACHE_WRITE_1H_KEY: "lots",
            },
        ),
        # 7. `True` is an `int` in Python and would store as a plausible 1.
        split_line(
            "msg_bool_value",
            31,
            flat=SPLIT_FLAT,
            cache_creation={
                ingest_mod.CACHE_WRITE_5M_KEY: True,
                ingest_mod.CACHE_WRITE_1H_KEY: SPLIT_1H,
            },
        ),
        # 8. Two STREAMED records of one call (#2). The split is a property of
        #    the CALL, so it must survive dedupe exactly once, carried by the
        #    surviving WHOLE record.
        split_line(
            "msg_streamed",
            37,
            flat=SPLIT_FLAT,
            cache_creation=both_ttls(),
            output_tokens=5,
        ),
        split_line(
            "msg_streamed",
            37,
            flat=SPLIT_FLAT,
            cache_creation=both_ttls(),
            output_tokens=9,
        ),
        # 9. Three streamed records of a call whose split is UNREADABLE, so the
        #    "without a split" and "unreadable" counts both range over a
        #    multi-record call too (see the note at 5). Three, not two, so the
        #    per-record count differs from the per-call one by more than the
        #    other shapes could mask.
        split_line("msg_streamed_unreadable", 47, flat=SPLIT_FLAT,
                   cache_creation=[SPLIT_5M], output_tokens=2),
        split_line("msg_streamed_unreadable", 47, flat=SPLIT_FLAT,
                   cache_creation=[SPLIT_5M], output_tokens=4),
        split_line("msg_streamed_unreadable", 47, flat=SPLIT_FLAT,
                   cache_creation=[SPLIT_5M], output_tokens=6),
    ]
)


class PerTtlCacheWriteIngestTest(unittest.TestCase):
    """`usage.cache_creation`, read onto the call at the `message.id` grain (#84).

    `cache_creation_input_tokens` is a FLAT TOTAL over two writes that are
    priced differently and repay after a different number of reads -- 1.25x
    repaid by the first read, 2x needing the second (TA-8). So "did this write
    repay?" has two answers and the flat total names neither.

    What has to stay apart here, and why one nullable column pair is enough
    only because NULL means exactly one thing:

      1. both keys read -> two measurements, and the flat total UNCHANGED
         beside them. It is what every existing figure reads.
      2. no `cache_creation` at all -> both NULL. UNMEASURED, never 0 (which
         would say this call wrote no cache at that TTL) and never a share of
         the flat total (which would be an apportionment nobody observed).
      3. one key only -> that one read, the other NULL. A half-measurement is
         still a measurement; the absent half is censused by name.
      4. a third key -> censused by name, folded into neither column.

    And the finding the fixture exists to keep visible: the split and the flat
    total DO NOT always sum, so nothing here reconciles them.
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cpb-ttl-split-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.projects = self.tmp / "projects"
        self.projects.mkdir()
        self.transcript = self.projects / "split-fixture.jsonl"
        self.transcript.write_text(SPLIT_TRANSCRIPT)
        self.db = self.tmp / "usage.db"
        self.summary = ingest(self.projects, self.db)
        self.conn = sqlite3.connect(self.db)
        self.addCleanup(self.conn.close)

    def _rows(self) -> dict:
        return {
            row[0]: row[1:]
            for row in self.conn.execute(
                "SELECT message_id, cache_write, cache_write_5m, cache_write_1h"
                " FROM api_calls"
            )
        }

    def test_every_record_parsed_and_one_row_survives_per_call(self) -> None:
        # 15 records, 11 calls: three ids are streamed over more than one
        # record and are ONE call each. Nothing here is malformed as a RECORD
        # -- an unreadable split is a refusal to measure a supplementary field,
        # not a parse failure, and must never cost the call its four token
        # classes.
        self.assertEqual(self.summary["unparsed_records"], 0)
        self.assertEqual(self.summary["records_parsed"], 15)
        self.assertEqual(len(self._rows()), 11)

    def test_each_shape_lands_on_its_call_with_the_flat_total_intact(self) -> None:
        # The whole acceptance table in one assertion, and the reason the
        # fixture's values are all distinct primes: every cell here would still
        # be a legal-looking number under a swapped or duplicated mapping.
        self.assertEqual(
            self._rows(),
            {
                "msg_both": (SPLIT_FLAT, SPLIT_5M, SPLIT_1H),
                # UNMEASURED, and the flat total survives untouched beside it.
                "msg_flat_only": (SPLIT_FLAT, None, None),
                "msg_only_5m": (SPLIT_5M, SPLIT_5M, None),
                "msg_only_1h": (SPLIT_1H, None, SPLIT_1H),
                # The third key changed neither stored column.
                "msg_third_ttl": (SPLIT_FLAT + UNKNOWN_TTL_VALUE, SPLIT_5M, SPLIT_1H),
                # As read. 0 + 1031 != 0, and it is stored that way.
                "msg_unreconciled": (0, 0, SPLIT_ORPHAN_1H),
                "msg_not_an_object": (SPLIT_FLAT, None, None),
                # The readable half stands; the unreadable one is not invented.
                "msg_bad_value": (SPLIT_FLAT, SPLIT_5M, None),
                # `True` is an int in Python and must NOT store as 1.
                "msg_bool_value": (SPLIT_FLAT, None, SPLIT_1H),
                "msg_streamed": (SPLIT_FLAT, SPLIT_5M, SPLIT_1H),
                "msg_streamed_unreadable": (SPLIT_FLAT, None, None),
            },
        )

    def test_the_two_ttls_are_not_interchangeable(self) -> None:
        # Stated on its own, because it is the assertion a swapped mapping has
        # to fail. The fixture pins them unequal precisely so this can be said.
        row = self._rows()["msg_both"]
        self.assertNotEqual(SPLIT_5M, SPLIT_1H)
        self.assertEqual(row[1], SPLIT_5M)
        self.assertEqual(row[2], SPLIT_1H)
        self.assertNotEqual(row[1], row[2])

    def test_an_absent_split_is_unmeasured_and_is_not_the_flat_total(self) -> None:
        # The rule this design exists for. The two wrong answers are named
        # explicitly so that neither can pass as "close enough": 0 states the
        # call wrote no cache at that TTL, and the flat total states it was all
        # 5-minute -- which for 26.86% of measured cache-write tokens
        # (2026-08-05) would apply a 1.25x break-even to a 2x write.
        flat, five, hour = self._rows()["msg_flat_only"]
        self.assertEqual(flat, SPLIT_FLAT)
        self.assertIsNone(five)
        self.assertIsNone(hour)
        self.assertNotEqual(five, 0)
        self.assertNotEqual(five, flat)

    def test_a_call_with_no_split_is_not_reachable_by_asking_for_zero(self) -> None:
        # The state change a reader actually depends on: SQL `= 0` must not
        # return the unmeasured rows, and `IS NULL` must not return the real
        # zero. `msg_unreconciled` carries a MEASURED 0 for 5-minute, so the
        # two readings are both present in this database and must not collide.
        def ids(where: str) -> set:
            return {
                row[0]
                for row in self.conn.execute(
                    f"SELECT message_id FROM api_calls WHERE {where}"
                )
            }

        self.assertEqual(ids("cache_write_5m = 0"), {"msg_unreconciled"})
        self.assertIn("msg_flat_only", ids("cache_write_5m IS NULL"))
        self.assertNotIn("msg_unreconciled", ids("cache_write_5m IS NULL"))

    def test_a_third_ttl_key_is_censused_by_name_and_not_folded_in(self) -> None:
        # `source_shape` already censuses a `usage` key nobody has seen (#15),
        # and this is exactly what it exists for -- so the name is QUALIFIED
        # rather than a parallel mechanism, and a nested key can never be
        # confused with a top-level one of the same spelling.
        self.assertEqual(
            census_rows(
                self.db, ingest_mod.SHAPE_UNKNOWN_USAGE_KEY, self.transcript
            ),
            {f"{ingest_mod.CACHE_CREATION_KEY}.{UNKNOWN_TTL_KEY}": 1},
        )
        # ...and its tokens went into NEITHER stored column, because CPB does
        # not know what they are. Folding them into 5-minute would price a
        # token class at a multiplier nobody documented.
        flat, five, hour = self._rows()["msg_third_ttl"]
        self.assertEqual((five, hour), (SPLIT_5M, SPLIT_1H))
        self.assertEqual(flat - (five + hour), UNKNOWN_TTL_VALUE)

    def test_a_ttl_key_that_goes_missing_is_censused_by_name(self) -> None:
        # The other direction, and it is counted even though it is SAFE: an
        # absent key here yields NULL rather than a silent 0, so nothing is
        # zeroed -- but a release that stopped writing one is still a format
        # move, and a column that quietly went all-NULL is not something anyone
        # spots in a chart. One record omits each key; the two records with no
        # `cache_creation` at all are NOT counted here (there is no object for
        # a key to be missing from), and neither is the non-object one.
        self.assertEqual(
            census_rows(
                self.db, ingest_mod.SHAPE_MISSING_USAGE_KEY, self.transcript
            ),
            {
                f"{ingest_mod.CACHE_CREATION_KEY}.{ingest_mod.CACHE_WRITE_5M_KEY}": 1,
                f"{ingest_mod.CACHE_CREATION_KEY}.{ingest_mod.CACHE_WRITE_1H_KEY}": 1,
            },
        )

    def test_the_split_and_the_flat_total_are_not_made_to_agree(self) -> None:
        # The finding, pinned. Measured on 8 of 170,079 deduped calls
        # (2026-08-05), every one of them flat-0 against a non-zero split, so
        # the flat total understated. Normalising either value would delete the
        # only evidence that they disagreed.
        flat, five, hour = self._rows()["msg_unreconciled"]
        self.assertEqual((flat, five, hour), (0, 0, SPLIT_ORPHAN_1H))
        self.assertNotEqual(five + hour, flat)
        # And a reader can find it in SQL, over the rows where BOTH TTLs are
        # measured -- which is the only set the question can be asked of.
        self.assertEqual(
            {
                row[0]
                for row in self.conn.execute(
                    "SELECT message_id FROM api_calls"
                    " WHERE cache_write_5m IS NOT NULL AND cache_write_1h IS NOT NULL"
                    " AND cache_write_5m + cache_write_1h != cache_write"
                )
            },
            {"msg_unreconciled", "msg_third_ttl"},
        )

    def test_a_partial_split_is_not_reported_as_a_disagreement(self) -> None:
        # With one TTL unmeasured the sum of what is known is not the sum of
        # what happened, so there is nothing to compare. Counting those calls
        # as unreconciled would invent a finding in the field added to prevent
        # exactly that -- `msg_only_5m` and `msg_only_1h` are half-measured,
        # not in disagreement.
        parsed = parse_file(self.transcript)
        by_id = {call.message_id: call for call in parsed.calls}
        for message_id in ("msg_only_5m", "msg_only_1h", "msg_bad_value"):
            with self.subTest(call=message_id):
                self.assertIsNone(by_id[message_id].cache_write_split_total)
                self.assertFalse(by_id[message_id].cache_write_unreconciled)

    def test_an_unreadable_split_costs_the_call_nothing_measured(self) -> None:
        # The usage object was fine; only the supplementary field was not.
        # Raising here -- as `tok()` does for a present non-numeric value --
        # would trade four measured token classes for a fifth.
        self.assertEqual(
            {
                row[0]: row[1:]
                for row in self.conn.execute(
                    "SELECT message_id, input_tokens, cache_read, cache_write,"
                    " output_tokens FROM api_calls WHERE message_id IN"
                    " ('msg_not_an_object', 'msg_bad_value')"
                )
            },
            {
                "msg_not_an_object": (23, 23 * 7, SPLIT_FLAT, 3),
                "msg_bad_value": (29, 29 * 7, SPLIT_FLAT, 3),
            },
        )

    def test_the_split_survives_dedupe_exactly_once(self) -> None:
        # #2: one API call is many transcript records, each repeating the same
        # `message.usage`. Counting records would inflate both TTL columns by
        # however many content blocks Claude Code happened to stream.
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM api_calls WHERE message_id = 'msg_streamed'"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT output_tokens, cache_write_5m, cache_write_1h FROM api_calls"
                " WHERE message_id = 'msg_streamed'"
            ).fetchone(),
            (9, SPLIT_5M, SPLIT_1H),
        )

    def test_the_split_joins_the_dedupe_agreement_check(self) -> None:
        # Measured 2026-08-05: 0 of 123,367 multi-record ids disagreed on the
        # split alone, so this changes no number today. It is here so that
        # stays a MEASURED fact -- a field CPB stores but does not compare is
        # one `divergent_message_ids` silently does not range over.
        self.assertIn("cache_write_5m", ingest_mod._STATIC_USAGE)
        self.assertIn("cache_write_1h", ingest_mod._STATIC_USAGE)
        source = self.projects / "divergent-split.jsonl"
        source.write_text(
            split_line("msg_split_drift", 41, flat=SPLIT_FLAT,
                       cache_creation=both_ttls(), output_tokens=2)
            + split_line("msg_split_drift", 41, flat=SPLIT_FLAT,
                         cache_creation=both_ttls(SPLIT_1H, SPLIT_5M),
                         output_tokens=6)
        )
        parsed = parse_file(source)
        self.assertEqual(parsed.divergent_message_ids, 1)
        # One WHOLE record survives -- never a per-field maximum, which would
        # describe an API response that never happened.
        self.assertEqual(len(parsed.calls), 1)
        self.assertEqual(
            (parsed.calls[0].cache_write_5m, parsed.calls[0].cache_write_1h),
            (SPLIT_1H, SPLIT_5M),
        )

    def test_the_three_coverage_facts_are_counted_separately(self) -> None:
        # Three questions, three counts. A single "problems" number would name
        # none of them, and folding the partial calls into "without a split"
        # would report a measurement as an absence.
        #
        # without: msg_flat_only, msg_not_an_object, msg_streamed_unreadable.
        # unreadable: those last two, plus msg_bad_value and msg_bool_value.
        # unreconciled: msg_unreconciled, msg_third_ttl.
        #
        # All three range over CALLS. Per RECORD they would read 5, 6 and 3
        # respectively, because two of the counted shapes are streamed -- which
        # is what makes the grain testable at all.
        self.assertEqual(self.summary["calls_without_cache_write_split"], 3)
        self.assertEqual(self.summary["calls_with_unreadable_cache_write_split"], 4)
        self.assertEqual(self.summary["calls_with_unreconciled_cache_write"], 2)

    def test_single_file_mode_reports_the_same_three_counts(self) -> None:
        # The hook path measures the same file and must not report a quieter
        # corpus than the directory path does.
        summary = ingest_mod.ingest_transcript(self.transcript, self.tmp / "hook.db")
        self.assertEqual(summary["calls_without_cache_write_split"], 3)
        self.assertEqual(summary["calls_with_unreadable_cache_write_split"], 4)
        self.assertEqual(summary["calls_with_unreconciled_cache_write"], 2)

    def test_the_counts_are_said_out_loud(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            ingest_mod.print_cache_write_split_note(self.summary)
        note = out.getvalue()
        self.assertIn("no `usage.cache_creation` split: 3", note)
        self.assertIn("unreadable: 4", note)
        self.assertIn("`cache_creation_input_tokens`: 2", note)
        # And it says what the numbers MEAN, because a bare count invites the
        # benign reading this whole design exists to refuse.
        self.assertIn("UNMEASURED", note)
        self.assertIn("assume all 5-minute", note)

    def test_a_corpus_with_nothing_to_report_says_nothing(self) -> None:
        # So that the note above MEANS something when it appears.
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            ingest_mod.print_cache_write_split_note(
                {
                    "calls_without_cache_write_split": 0,
                    "calls_with_unreadable_cache_write_split": 0,
                    "calls_with_unreconciled_cache_write": 0,
                }
            )
        self.assertEqual(out.getvalue(), "")

    def test_a_clean_split_names_nothing_in_the_census(self) -> None:
        # The census must stay silent on the shape CPB expects, or every real
        # corpus reports a shape change and the signal is worthless.
        clean = self.projects / "clean-split.jsonl"
        clean.write_text(
            split_line("msg_clean", 43, flat=SPLIT_FLAT, cache_creation=both_ttls())
        )
        parsed = parse_file(clean)
        self.assertEqual(
            {
                fact: name
                for (fact, name) in parsed.shape
                if fact != ingest_mod.SHAPE_VERSION
            },
            {},
        )


class PerTtlCacheWriteUpgradeTest(unittest.TestCase):
    """v11 -> v12 adds the two columns without re-parsing anything (#84).

    The rows a v11 database holds were written by a build that never looked
    inside `usage.cache_creation`. They upgrade to NULL, which is UNMEASURED --
    the only true statement about them.

    The two back-fills that would have been convenient are both FALSE, and
    falsely precise, which is what makes this hop's admission to
    `IN_PLACE_UPGRADE_FROM` an argument rather than a habit:

      * 0 states the call wrote no cache at that TTL. Wrong for 131,662 of
        170,079 calls on the reference corpus (2026-08-05).
      * `cache_write_5m = cache_write` -- "assume all 5-minute" -- is TA-8's
        forbidden collapse applied to a whole corpus at once, and it errs in
        the direction that makes a 2x write look like a 1.25x one, i.e. it
        would report writes as repaid that were not. 26.86% of measured
        cache-write tokens were 1-hour.

    The alternative to an in-place hop is a rebuild, which the reaped-source
    guard REFUSES on exactly the corpora whose database is the only copy of
    their history. Two added nullable columns must not be the change that
    strands those databases at v11.
    """

    # The v11 shape of `api_calls`, STATED rather than reached by DROP COLUMN.
    # Same reasoning as `CacheMissDiagnosticsUpgradeTest.API_CALLS_AT_V10`: the
    # DROP COLUMN statement is itself version-sensitive, and a fixture that
    # differs across CI legs tests the fixture rather than the code.
    API_CALLS_AT_V11 = """
        CREATE TABLE api_calls_v11 (
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
            message_id TEXT,
            cache_miss_outcome TEXT,
            cache_miss_reason TEXT,
            cache_missed_input_tokens INTEGER
        )
    """
    V11_COLUMNS = (
        "id, session_id, source_path, source_kind, agent_id, turn_id, ts, model,"
        " input_tokens, cache_read, cache_write, output_tokens, context_size,"
        " is_sidechain, message_id, cache_miss_outcome, cache_miss_reason,"
        " cache_missed_input_tokens"
    )

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cpb-ttl-split-upgrade-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.projects = self.tmp / "projects"
        self.projects.mkdir()
        self.transcript = self.projects / "split-fixture.jsonl"
        self.transcript.write_text(SPLIT_TRANSCRIPT)
        self.db = self.tmp / "usage.db"
        ingest(self.projects, self.db)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db)
        self.addCleanup(conn.close)
        return conn

    def _snapshot(self) -> list:
        return self._conn().execute(
            f"SELECT {self.V11_COLUMNS} FROM api_calls ORDER BY id"
        ).fetchall()

    def _split_states(self) -> dict:
        """How many rows are unmeasured, and how many carry a reading."""
        conn = self._conn()
        return {
            row[0]: row[1]
            for row in conn.execute(
                "SELECT CASE WHEN cache_write_5m IS NULL AND cache_write_1h IS NULL"
                " THEN '(unmeasured)' ELSE '(read)' END s, COUNT(*)"
                " FROM api_calls GROUP BY s"
            )
        }

    def _downgrade_to_v11(self) -> None:
        conn = self._conn()
        conn.execute(self.API_CALLS_AT_V11)
        conn.execute(
            f"INSERT INTO api_calls_v11 SELECT {self.V11_COLUMNS} FROM api_calls"
        )
        conn.execute("DROP TABLE api_calls")
        conn.execute("ALTER TABLE api_calls_v11 RENAME TO api_calls")
        conn.execute("PRAGMA user_version = 11")
        conn.commit()
        self.assertNotIn("cache_write_5m", ingest_mod._table_columns(conn, "api_calls"))

    def test_a_fresh_ingest_reads_the_split_on_the_calls_that_carry_one(self) -> None:
        # The baseline the upgrade must NOT reproduce.
        self.assertEqual(self._split_states(), {"(read)": 8, "(unmeasured)": 3})

    def test_the_upgrade_carries_the_rows_and_leaves_them_unmeasured(self) -> None:
        self._downgrade_to_v11()
        before = self._snapshot()
        self.assertTrue(before, "the fixture must hold rows for this to mean anything")

        summary = ingest(self.projects, self.db)

        self.assertFalse(summary["schema_rebuilt"])
        # A rebuild empties `ingest_state`, so every file would be re-parsed.
        # Skipping them all is the observable proof the rows were carried.
        self.assertEqual(summary["files_ingested"], 0)
        self.assertEqual(summary["files_skipped"], summary["files_scanned"])
        self.assertEqual(self._snapshot(), before)
        self.assertEqual(
            self._conn().execute("PRAGMA user_version").fetchone()[0],
            ingest_mod.SCHEMA_VERSION,
        )
        # EVERY carried row reads unmeasured -- not 0, not the flat total.
        self.assertEqual(self._split_states(), {"(unmeasured)": len(before)})

    def test_no_carried_row_is_back_filled_with_a_value(self) -> None:
        # Stated as its own assertion because it is the one a "helpful"
        # migration would break, and it would break it plausibly: every row
        # would carry a number, and nothing on the page would say it was
        # manufactured.
        self._downgrade_to_v11()
        ingest(self.projects, self.db)
        self.assertEqual(
            self._conn().execute(
                "SELECT COUNT(*) FROM api_calls WHERE cache_write_5m IS NOT NULL"
                " OR cache_write_1h IS NOT NULL"
            ).fetchone()[0],
            0,
        )
        # ...and specifically not from the column sitting right beside them.
        self.assertEqual(
            self._conn().execute(
                "SELECT COUNT(*) FROM api_calls WHERE cache_write_5m = cache_write"
            ).fetchone()[0],
            0,
        )

    def test_the_flat_total_is_untouched_by_the_upgrade(self) -> None:
        # The acceptance rule that makes this a non-breaking change: every
        # existing figure reads `cache_write`, and the hop must not move one.
        self._downgrade_to_v11()
        before = {
            row[0]: row[1]
            for row in self._conn().execute(
                "SELECT message_id, cache_write FROM api_calls"
            )
        }
        ingest(self.projects, self.db)
        self.assertEqual(
            {
                row[0]: row[1]
                for row in self._conn().execute(
                    "SELECT message_id, cache_write FROM api_calls"
                )
            },
            before,
        )

    def test_the_upgrade_is_not_refused_over_a_reaped_source(self) -> None:
        # Past Claude Code's retention window the database is the only copy of
        # its measurements, and a rebuild would be REFUSED. Two added nullable
        # columns must not be the change that strands those databases at v11.
        self.transcript.unlink()
        ingest(self.projects, self.db)  # archives it; rows retained
        self._downgrade_to_v11()
        before = self._snapshot()

        ingest(self.projects, self.db)  # must not raise SystemExit

        self.assertEqual(self._snapshot(), before)
        self.assertEqual(self._split_states(), {"(unmeasured)": len(before)})

    def test_re_ingesting_the_source_replaces_unmeasured_with_a_reading(self) -> None:
        # The other half of "not back-filled with a guess": the answer arrives
        # when the file is read again, which is exactly when it can be known.
        self._downgrade_to_v11()
        ingest(self.projects, self.db)
        self.assertEqual(self._split_states(), {"(unmeasured)": 11})

        self.transcript.write_text(SPLIT_TRANSCRIPT)  # changes mtime/size
        os.utime(self.transcript, (time.time() + 2, time.time() + 2))
        ingest(self.projects, self.db)

        self.assertEqual(self._split_states(), {"(read)": 8, "(unmeasured)": 3})


if __name__ == "__main__":
    unittest.main()
