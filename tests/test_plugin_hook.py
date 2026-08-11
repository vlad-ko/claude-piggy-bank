"""Tests for the plugin's ingest hook handler (`hooks/cpb_ingest_hook.py`).

The handler is the only executable code the plugin ships. Everything it does
is decided before `ingest.py` is ever spawned -- which transcript to read,
which database to write, and what to do when either answer is unavailable --
so all of it is testable without touching a real transcript or a real DB. The
subprocess is injected in every test here; none of these spawn a process.

Two properties carry most of the weight:

- **`SubagentStop` must ingest `agent_transcript_path`, not `transcript_path`.**
  On that event `transcript_path` is the *parent session's* transcript, so a
  handler that read it would ingest the main thread a second time, report
  success, and never capture the subagent -- the exact spend the hook exists to
  save from reaping. The fixture therefore supplies both fields, pointing at
  two different real files, so reading the wrong one cannot pass.
- **The handler must never exit 2.** On `Stop` exit code 2 prevents Claude from
  stopping and on `SubagentStop` it prevents the subagent from stopping, so a
  measurement tool that returned 2 on failure would hijack the user's session.
  Every failure path below asserts the code is 1, and a sweep asserts no input
  produces 2.

The module is loaded by path rather than imported by name on purpose: the
`stdlib-only` CI job treats any import that is not stdlib and not a top-level
or `tests/` module as a third-party dependency, and `hooks/` is neither.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
HANDLER = REPO_ROOT / "hooks" / "cpb_ingest_hook.py"

# `serve.py` is imported here for exactly one class -- `HookToReportSeamTest`,
# which asks the report what it says about a database the hook built. The seam
# between the two paths is the thing #105 lived in, and it can only be asserted
# by a test that can see both ends of it.
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import serve  # noqa: E402  (must follow the sys.path insertion above)


def load_handler():
    """Load the hook handler from its path in the plugin tree."""
    spec = importlib.util.spec_from_file_location("cpb_ingest_hook", HANDLER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


hook = load_handler()


class RecordingRunner:
    """Stands in for `subprocess.run`, recording what would have been spawned.

    `returncode` and `raises` are set per test so exit-code propagation and the
    timeout path can be exercised without a real child process.
    """

    def __init__(self, returncode: int = 0, stderr: str = "", raises=None):
        self.returncode = returncode
        self.stderr = stderr
        self.raises = raises
        self.calls: list[dict] = []

    def __call__(self, argv, *, env, timeout):
        self.calls.append({"argv": list(argv), "env": dict(env), "timeout": timeout})
        if self.raises is not None:
            raise self.raises
        return subprocess.CompletedProcess(argv, self.returncode, "", self.stderr)

    @property
    def argv(self) -> list[str]:
        self_calls = self.calls
        assert len(self_calls) == 1, f"expected exactly one spawn, got {len(self_calls)}"
        return self_calls[0]["argv"]

    @property
    def env(self) -> dict:
        assert len(self.calls) == 1, f"expected exactly one spawn, got {len(self.calls)}"
        return self.calls[0]["env"]


class ScriptedRunner(RecordingRunner):
    """A runner whose result differs per attempt, for the retry tests.

    Takes a list of `(returncode, stderr)` pairs, one per expected spawn. Runs
    off the end of the script deliberately loudly rather than repeating the
    last result, so a handler that retried more times than intended cannot pass.
    """

    def __init__(self, results):
        super().__init__()
        self.results = list(results)

    def __call__(self, argv, *, env, timeout):
        self.calls.append({"argv": list(argv), "env": dict(env), "timeout": timeout})
        index = len(self.calls) - 1
        assert index < len(self.results), (
            f"spawned {index + 1} times; the script has {len(self.results)} results"
        )
        returncode, stderr = self.results[index]
        return subprocess.CompletedProcess(argv, returncode, "", stderr)


class HookTestCase(unittest.TestCase):
    """Shared scaffolding: a temp dir, real .jsonl files, and a `run` helper."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.data_dir = self.tmp / "plugin-data"
        self.session_transcript = self.write_transcript("session.jsonl")
        self.agent_transcript = self.write_transcript("agent-def456.jsonl")

    def write_transcript(self, name: str) -> Path:
        path = self.tmp / name
        path.write_text('{"type": "assistant"}\n', encoding="utf-8")
        return path

    def run_hook(self, payload, *, env=None, runner=None, raw_stdin=None):
        """Invoke `main()` with everything injected. Returns (code, stderr, runner)."""
        runner = RecordingRunner() if runner is None else runner
        stderr = io.StringIO()
        if env is None:
            env = {
                "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT),
                "CLAUDE_PLUGIN_DATA": str(self.data_dir),
            }
        text = raw_stdin if raw_stdin is not None else json.dumps(payload)
        self.slept = []
        code = hook.main(
            stdin=io.StringIO(text),
            env=env,
            runner=runner,
            stderr=stderr,
            sleeper=self.slept.append,
        )
        return code, stderr.getvalue(), runner

    def stop_payload(self, **overrides):
        payload = {
            "session_id": "abc123",
            "transcript_path": str(self.session_transcript),
            "cwd": str(self.tmp),
            "hook_event_name": "Stop",
        }
        payload.update(overrides)
        return payload

    def subagent_stop_payload(self, **overrides):
        payload = {
            "session_id": "abc123",
            "transcript_path": str(self.session_transcript),
            "cwd": str(self.tmp),
            "hook_event_name": "SubagentStop",
            "agent_id": "def456",
            "agent_type": "Explore",
            "agent_transcript_path": str(self.agent_transcript),
        }
        payload.update(overrides)
        return payload


class TranscriptSelectionTest(HookTestCase):
    """Which transcript each event ingests. Reading the wrong one is silent."""

    def test_subagent_stop_ingests_the_agent_transcript_not_the_session_one(self):
        # The whole point of the SubagentStop trigger. The payload carries both
        # paths; only `agent_transcript_path` is the subagent's own transcript.
        code, _, runner = self.run_hook(self.subagent_stop_payload())

        self.assertEqual(code, 0)
        self.assertIn(str(self.agent_transcript), runner.argv)
        self.assertNotIn(str(self.session_transcript), runner.argv)

    def test_stop_ingests_the_session_transcript(self):
        code, _, runner = self.run_hook(self.stop_payload())

        self.assertEqual(code, 0)
        self.assertIn(str(self.session_transcript), runner.argv)

    def test_session_end_ingests_the_session_transcript(self):
        payload = self.stop_payload(hook_event_name="SessionEnd", reason="other")
        del payload["transcript_path"]
        payload["transcript_path"] = str(self.session_transcript)

        code, _, runner = self.run_hook(payload)

        self.assertEqual(code, 0)
        self.assertIn(str(self.session_transcript), runner.argv)

    def test_argv_passes_the_transcript_through_the_documented_flag(self):
        code, _, runner = self.run_hook(self.stop_payload())

        self.assertEqual(code, 0)
        argv = runner.argv
        self.assertEqual(argv[0], sys.executable)
        self.assertEqual(argv[1], str(REPO_ROOT / "ingest.py"))
        self.assertEqual(argv[2], "--transcript")
        self.assertEqual(argv[3], str(self.session_transcript))

    def test_subagent_stop_without_an_agent_transcript_refuses_rather_than_falling_back(self):
        # Falling back to `transcript_path` would ingest the parent session and
        # report success while the subagent's spend went unmeasured. Absence is
        # not a value: refuse and say so.
        payload = self.subagent_stop_payload()
        del payload["agent_transcript_path"]

        code, stderr, runner = self.run_hook(payload)

        self.assertEqual(code, 1)
        self.assertEqual(runner.calls, [])
        self.assertIn("agent_transcript_path", stderr)

    def test_user_home_shorthand_in_the_path_is_expanded(self):
        # The documented SubagentStop and Stop payload examples show
        # `~/.claude/projects/...`; a literal "~" directory would never exist.
        home = self.tmp / "home"
        (home / "nested").mkdir(parents=True)
        transcript = home / "nested" / "s.jsonl"
        transcript.write_text("{}\n", encoding="utf-8")

        with unittest.mock.patch.dict(
            os.environ, {"HOME": str(home), "USERPROFILE": str(home)}
        ):
            code, stderr, runner = self.run_hook(
                self.stop_payload(transcript_path="~/nested/s.jsonl")
            )

        self.assertEqual(code, 0, stderr)
        self.assertIn(str(transcript), runner.argv)


class TranscriptValidationTest(HookTestCase):
    """Every way `transcript_path` can be unusable, handled explicitly."""

    def assert_refused(self, payload, *, expected_in_stderr: str):
        code, stderr, runner = self.run_hook(payload)
        self.assertEqual(code, 1)
        self.assertEqual(runner.calls, [], "refused inputs must not spawn ingest")
        self.assertIn(expected_in_stderr, stderr)
        return stderr

    def test_absent_field_refuses(self):
        payload = self.stop_payload()
        del payload["transcript_path"]
        self.assert_refused(payload, expected_in_stderr="transcript_path")

    def test_empty_string_refuses(self):
        self.assert_refused(
            self.stop_payload(transcript_path=""), expected_in_stderr="transcript_path"
        )

    def test_whitespace_only_refuses(self):
        self.assert_refused(
            self.stop_payload(transcript_path="   "), expected_in_stderr="transcript_path"
        )

    def test_non_string_refuses(self):
        self.assert_refused(
            self.stop_payload(transcript_path=["a"]), expected_in_stderr="transcript_path"
        )

    def test_relative_path_refuses_rather_than_resolving_against_cwd(self):
        # Hooks run in Claude Code's current directory, which is the user's
        # project, not the transcript store. Joining a relative path onto it
        # would be a guess.
        self.assert_refused(
            self.stop_payload(transcript_path="projects/s.jsonl"),
            expected_in_stderr="absolute",
        )

    def test_missing_file_refuses(self):
        self.assert_refused(
            self.stop_payload(transcript_path=str(self.tmp / "gone.jsonl")),
            expected_in_stderr="does not exist",
        )

    def test_directory_refuses(self):
        directory = self.tmp / "dir.jsonl"
        directory.mkdir()
        self.assert_refused(
            self.stop_payload(transcript_path=str(directory)),
            expected_in_stderr="does not exist",
        )

    def test_non_jsonl_suffix_refuses(self):
        other = self.tmp / "notes.txt"
        other.write_text("hello\n", encoding="utf-8")
        self.assert_refused(
            self.stop_payload(transcript_path=str(other)), expected_in_stderr=".jsonl"
        )


class StdinParsingTest(HookTestCase):
    """The hook payload arrives on stdin and may not be what we expect."""

    def test_malformed_json_refuses(self):
        code, stderr, runner = self.run_hook(None, raw_stdin="{not json")

        self.assertEqual(code, 1)
        self.assertEqual(runner.calls, [])
        self.assertIn("stdin", stderr)

    def test_empty_stdin_refuses(self):
        code, stderr, runner = self.run_hook(None, raw_stdin="")

        self.assertEqual(code, 1)
        self.assertEqual(runner.calls, [])
        self.assertIn("stdin", stderr)

    def test_json_that_is_not_an_object_refuses(self):
        code, stderr, runner = self.run_hook(None, raw_stdin="[1, 2, 3]")

        self.assertEqual(code, 1)
        self.assertEqual(runner.calls, [])
        self.assertIn("stdin", stderr)


class DatabaseLocationTest(HookTestCase):
    """Where the hook tells ingest to write, and when it refuses to decide."""

    def test_plugin_data_dir_supplies_cpb_db(self):
        code, stderr, runner = self.run_hook(self.stop_payload())

        self.assertEqual(code, 0, stderr)
        self.assertEqual(runner.env["CPB_DB"], str(self.data_dir / "usage.db"))

    def test_the_database_directory_is_created(self):
        self.assertFalse(self.data_dir.exists())

        code, stderr, _ = self.run_hook(self.stop_payload())

        self.assertEqual(code, 0, stderr)
        self.assertTrue(self.data_dir.is_dir())

    def test_an_existing_cpb_db_setting_is_not_overridden(self):
        chosen = self.tmp / "mine" / "cpb.db"
        env = {
            "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT),
            "CLAUDE_PLUGIN_DATA": str(self.data_dir),
            "CPB_DB": str(chosen),
        }

        code, stderr, runner = self.run_hook(self.stop_payload(), env=env)

        self.assertEqual(code, 0, stderr)
        self.assertEqual(runner.env["CPB_DB"], str(chosen))

    def test_plugin_root_without_a_data_dir_refuses(self):
        # ${CLAUDE_PLUGIN_ROOT} is wiped on the next plugin update. The DB is
        # the only surviving copy of history past Claude Code's ~30-day reap,
        # so writing it there would silently destroy measurements. Refuse.
        env = {"CLAUDE_PLUGIN_ROOT": str(REPO_ROOT)}

        code, stderr, runner = self.run_hook(self.stop_payload(), env=env)

        self.assertEqual(code, 1)
        self.assertEqual(runner.calls, [])
        self.assertIn("CLAUDE_PLUGIN_DATA", stderr)

    def test_outside_a_plugin_no_cpb_db_is_injected(self):
        # Run from a checkout rather than an installed plugin, ingest.py's own
        # default (db/usage.db beside it) is correct and durable.
        code, stderr, runner = self.run_hook(self.stop_payload(), env={})

        self.assertEqual(code, 0, stderr)
        self.assertNotIn("CPB_DB", runner.env)


class ExitCodeTest(HookTestCase):
    """Failure must be loud and must never take the session with it."""

    def test_a_failed_ingest_exits_one_not_two(self):
        runner = RecordingRunner(returncode=3, stderr="ingest blew up\n")

        code, stderr, _ = self.run_hook(self.stop_payload(), runner=runner)

        self.assertEqual(code, 1)
        self.assertIn("ingest blew up", stderr)

    def test_a_timed_out_ingest_exits_one(self):
        runner = RecordingRunner(raises=subprocess.TimeoutExpired(cmd="ingest.py", timeout=20))

        code, stderr, _ = self.run_hook(self.stop_payload(), runner=runner)

        self.assertEqual(code, 1)
        self.assertIn("timed out", stderr)

    def test_the_spawn_is_bounded_by_an_explicit_timeout(self):
        code, _, runner = self.run_hook(self.stop_payload())

        self.assertEqual(code, 0)
        timeout = runner.calls[0]["timeout"]
        self.assertIsNotNone(timeout, "an unbounded ingest could hang the session")
        self.assertGreater(timeout, 0)
        self.assertLessEqual(timeout, 60)

    def test_an_unspawnable_ingest_exits_one(self):
        runner = RecordingRunner(raises=OSError("no such executable"))

        code, stderr, _ = self.run_hook(self.stop_payload(), runner=runner)

        self.assertEqual(code, 1)
        self.assertIn("no such executable", stderr)

    def test_no_input_produces_the_blocking_exit_code(self):
        # Exit 2 on Stop prevents Claude from stopping; on SubagentStop it
        # prevents the subagent from stopping. A measurement tool must never
        # reach either.
        no_transcript = self.stop_payload()
        del no_transcript["transcript_path"]
        cases = [
            ("malformed stdin", {"raw_stdin": "{"}),
            ("empty stdin", {"raw_stdin": ""}),
            ("no transcript", {"payload": no_transcript}),
            ("missing file", {"payload": self.stop_payload(transcript_path="/nope/x.jsonl")}),
            ("relative", {"payload": self.stop_payload(transcript_path="x.jsonl")}),
            ("failed ingest", {"payload": self.stop_payload(), "rc": 9}),
            (
                "no data dir",
                {"payload": self.stop_payload(), "env": {"CLAUDE_PLUGIN_ROOT": "/x"}},
            ),
        ]

        for label, case in cases:
            with self.subTest(label):
                runner = RecordingRunner(returncode=case.get("rc", 0))
                code, _, _ = self.run_hook(
                    case.get("payload"),
                    env=case.get("env"),
                    runner=runner,
                    raw_stdin=case.get("raw_stdin"),
                )
                self.assertIn(code, (0, 1), f"{label} produced exit {code}")
                self.assertNotEqual(code, hook.EXIT_BLOCKING)

    def test_the_operator_message_is_a_single_line(self):
        # Claude Code surfaces only the FIRST line of stderr in the transcript
        # notice. A message that wraps loses everything after the newline.
        runner = RecordingRunner(returncode=1, stderr="line one\nline two\nline three\n")

        code, stderr, _ = self.run_hook(self.stop_payload(), runner=runner)

        self.assertEqual(code, 1)
        self.assertEqual(len(stderr.strip().splitlines()), 1, stderr)
        self.assertIn("line two", stderr)


class ContentionRetryTest(HookTestCase):
    """Lock contention is transient; every other failure is not.

    `Stop` and `SubagentStop` can fire close together on one SQLite file, and
    `ingest.py` surfaces contention past sqlite3's 5-second busy timeout as a
    non-zero exit. Reporting that as a failure would spend the loud channel on
    a condition that resolves itself -- and a notice users learn to ignore is
    worse than no notice. Retrying anything else would paper over a real fault.
    """

    LOCKED = "sqlite3.OperationalError: database is locked"

    def test_contention_is_retried_and_the_second_attempt_can_succeed(self):
        runner = ScriptedRunner([(1, self.LOCKED), (0, "")])

        code, stderr, _ = self.run_hook(self.stop_payload(), runner=runner)

        self.assertEqual(code, 0, stderr)
        self.assertEqual(len(runner.calls), 2)
        self.assertEqual(stderr, "", "a recovered retry is not an operator event")

    def test_the_retry_backs_off_before_trying_again(self):
        runner = ScriptedRunner([(1, self.LOCKED), (0, "")])

        self.run_hook(self.stop_payload(), runner=runner)

        self.assertEqual(len(self.slept), 1)
        self.assertGreater(self.slept[0], 0)

    def test_retrying_is_bounded_not_indefinite(self):
        # Silently retrying until the lock clears would convert a loud failure
        # into a hang, which is the one outcome worse than a false alarm.
        runner = ScriptedRunner([(1, self.LOCKED), (1, self.LOCKED)])

        code, stderr, _ = self.run_hook(self.stop_payload(), runner=runner)

        self.assertEqual(code, 1)
        self.assertEqual(len(runner.calls), hook.MAX_INGEST_ATTEMPTS)

    def test_persistent_contention_stays_distinguishable_from_a_real_fault(self):
        runner = ScriptedRunner([(1, self.LOCKED), (1, self.LOCKED)])

        code, stderr, _ = self.run_hook(self.stop_payload(), runner=runner)

        self.assertEqual(code, 1)
        self.assertIn("contention", stderr.lower())
        self.assertIn(str(hook.MAX_INGEST_ATTEMPTS), stderr)

    def test_a_real_fault_is_reported_on_the_first_attempt_without_retrying(self):
        runner = ScriptedRunner([(1, "transcript not found: /x/y.jsonl")])

        code, stderr, _ = self.run_hook(self.stop_payload(), runner=runner)

        self.assertEqual(code, 1)
        self.assertEqual(len(runner.calls), 1, "a real fault must not be retried")
        self.assertNotIn("contention", stderr.lower())
        self.assertIn("transcript not found", stderr)
        self.assertEqual(self.slept, [])

    def test_the_retry_shares_one_budget_rather_than_doubling_it(self):
        # Two attempts each granted the full timeout would let the hook run for
        # twice its declared bound, past the timeout Claude Code enforces.
        runner = ScriptedRunner([(1, self.LOCKED), (0, "")])

        self.run_hook(self.stop_payload(), runner=runner)

        first, second = (call["timeout"] for call in runner.calls)
        self.assertLessEqual(first, hook.INGEST_TIMEOUT_SECONDS)
        self.assertLess(second, first, "the second attempt must draw on what is left")
        self.assertGreater(second, 0)


class DatabaseEnvHygieneTest(HookTestCase):
    """`ingest.py` refuses an empty `CPB_DB` rather than falling back."""

    def test_an_empty_inherited_cpb_db_is_replaced_not_passed_through(self):
        env = {
            "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT),
            "CLAUDE_PLUGIN_DATA": str(self.data_dir),
            "CPB_DB": "",
        }

        code, stderr, runner = self.run_hook(self.stop_payload(), env=env)

        self.assertEqual(code, 0, stderr)
        self.assertEqual(runner.env["CPB_DB"], str(self.data_dir / "usage.db"))

    def test_an_empty_inherited_cpb_db_is_removed_when_we_have_nothing_to_put_there(self):
        # Outside a plugin we have no path to supply. Passing the empty string
        # on would make ingest.py refuse, turning an inherited stray variable
        # into a per-turn failure with a confusing cause.
        code, stderr, runner = self.run_hook(self.stop_payload(), env={"CPB_DB": "   "})

        self.assertEqual(code, 0, stderr)
        self.assertNotIn("CPB_DB", runner.env)


class FailureLogTest(HookTestCase):
    """A failure has to outlive the transcript notice that announced it."""

    @property
    def log(self) -> Path:
        return self.data_dir / hook.LOG_FILENAME

    def test_a_failure_is_recorded_in_the_persistent_data_directory(self):
        runner = RecordingRunner(returncode=4, stderr="disk on fire")

        code, _, _ = self.run_hook(self.stop_payload(), runner=runner)

        self.assertEqual(code, 1)
        self.assertTrue(self.log.is_file())
        self.assertIn("disk on fire", self.log.read_text(encoding="utf-8"))

    def test_a_refusal_is_recorded_too(self):
        payload = self.stop_payload()
        del payload["transcript_path"]

        self.run_hook(payload)

        self.assertIn("transcript_path", self.log.read_text(encoding="utf-8"))

    def test_success_writes_nothing(self):
        # The Stop trigger fires on every turn. A success line per turn would
        # grow without bound in a directory that survives plugin updates.
        code, _, _ = self.run_hook(self.stop_payload())

        self.assertEqual(code, 0)
        self.assertFalse(self.log.exists())

    def test_the_log_is_rotated_at_the_size_cap(self):
        self.data_dir.mkdir(parents=True)
        self.log.write_text("x" * (hook.LOG_MAX_BYTES + 1), encoding="utf-8")
        runner = RecordingRunner(returncode=4, stderr="fresh failure")

        self.run_hook(self.stop_payload(), runner=runner)

        rotated = self.data_dir / (hook.LOG_FILENAME + ".1")
        self.assertTrue(rotated.is_file(), "the previous log must be kept, not dropped")
        self.assertLess(self.log.stat().st_size, hook.LOG_MAX_BYTES)
        self.assertIn("fresh failure", self.log.read_text(encoding="utf-8"))

    def test_an_unwritable_log_does_not_crash_the_hook(self):
        # The log is a durability courtesy layered on top of stderr, which has
        # already carried the message. Failing to write it must not turn a
        # reported failure into an unhandled traceback.
        self.data_dir.mkdir(parents=True)
        self.log.mkdir()  # a directory where the log file should be
        runner = RecordingRunner(returncode=4, stderr="disk on fire")

        code, stderr, _ = self.run_hook(self.stop_payload(), runner=runner)

        self.assertEqual(code, 1)
        self.assertIn("disk on fire", stderr)

    def test_an_unwritable_log_does_not_turn_a_success_into_a_failure(self):
        self.data_dir.mkdir(parents=True)
        self.log.mkdir()

        code, stderr, _ = self.run_hook(self.stop_payload())

        self.assertEqual(code, 0, stderr)


class HookSuccessRecordTest(HookTestCase):
    """A hook that never launches leaves nothing behind (#116).

    That is the whole defect: on a machine where `hooks.json`'s `python3` does
    not resolve, all three triggers fail before this module runs, and the
    report cannot tell that install from one created a minute ago. Nothing here
    can fix the launch -- see `docs/plugin.md` -- but a SUCCESS can leave a
    positive record, and then the absence of one is a fact the report can read.

    The failure log is the sibling rule and it is deliberately not this one:
    failures are APPENDED, because each is a separate event worth keeping;
    successes OVERWRITE, because `Stop` fires every turn and an append per turn
    would grow without bound in a directory that survives plugin updates.
    """

    @property
    def record(self) -> Path:
        return self.data_dir / hook.HOOK_STATE_FILENAME

    def read(self) -> dict:
        return json.loads(self.record.read_text(encoding="utf-8"))

    def test_a_successful_run_records_when_it_happened(self):
        before = time.time()

        code, _, _ = self.run_hook(self.stop_payload())

        self.assertEqual(code, 0)
        self.assertTrue(self.record.is_file(), "a success left no record of itself")
        self.assertGreaterEqual(self.read()["last_success_at"], before)

    def test_the_record_names_the_interpreter_the_launcher_resolved(self):
        # The half of #116's pair that could not be observed before. The
        # launcher runs a bare `python3` off PATH and this module resolves its
        # own child with `sys.executable`; recording the second is what lets
        # the report state which interpreter the first actually found.
        self.run_hook(self.stop_payload())

        self.assertEqual(self.read()["interpreter"], sys.executable)

    def test_a_failed_ingest_records_no_success(self):
        # The state the whole feature is about must not be reachable by a hook
        # that ran and failed. That failure has its own channel -- stderr and
        # the log -- and writing a success beside it would report the ingest
        # that did not happen.
        runner = RecordingRunner(returncode=4, stderr="disk on fire")

        code, _, _ = self.run_hook(self.stop_payload(), runner=runner)

        self.assertEqual(code, 1)
        self.assertFalse(self.record.exists())
        self.assertTrue((self.data_dir / hook.LOG_FILENAME).is_file())

    def test_a_refusal_records_no_success(self):
        payload = self.stop_payload()
        del payload["transcript_path"]

        self.run_hook(payload)

        self.assertFalse(self.record.exists())

    #: One JSON object of two small fields. Well above what that costs and far
    #: below what appending one line per turn would reach.
    RECORD_SIZE_CEILING = 256

    def test_the_record_is_overwritten_rather_than_grown(self):
        # The objection to logging successes, answered rather than ignored: one
        # record however many turns run, and the newest one wins. Asserted on
        # the SHAPE rather than on an exact size, which floats by a byte with
        # the timestamp's repr -- and on the parse, because an appended file
        # stops being a JSON object at the second turn.
        self.run_hook(self.stop_payload())
        first = self.read()["last_success_at"]
        time.sleep(0.01)

        for _ in range(3):
            self.run_hook(self.stop_payload())

        self.assertGreater(self.read()["last_success_at"], first)
        self.assertLess(self.record.stat().st_size, self.RECORD_SIZE_CEILING)
        self.assertEqual(
            sorted(p.name for p in self.data_dir.iterdir()),
            [hook.HOOK_STATE_FILENAME],
            "a scratch file was left behind beside the record",
        )

    def test_it_is_written_beside_the_database_the_run_ingested_into(self):
        # The rule the report depends on: `serve.py` is handed a database and
        # nothing else, so it looks for this file beside that database. A
        # record written anywhere else is a record with no reader.
        elsewhere = self.tmp / "chosen" / "mine.db"
        env = {
            "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT),
            "CLAUDE_PLUGIN_DATA": str(self.data_dir),
            "CPB_DB": str(elsewhere),
        }

        code, _, _ = self.run_hook(self.stop_payload(), env=env)

        self.assertEqual(code, 0)
        self.assertFalse(self.record.exists(), "written beside the log, not the DB")
        self.assertTrue((elsewhere.parent / hook.HOOK_STATE_FILENAME).is_file())

    def test_a_checkout_run_records_nothing_and_reports_nothing(self):
        # No plugin data directory and no `CPB_DB`: a plain checkout, where the
        # report says "not a plugin install" from its own location and never
        # consults this file. Writing one would be a record with no reader; a
        # failure to write one would be a complaint about nothing.
        code, stderr, _ = self.run_hook(self.stop_payload(), env={})

        self.assertEqual(code, 0, stderr)
        self.assertFalse(self.record.exists())
        self.assertEqual(stderr, "")

    def test_an_unwritable_data_directory_does_not_turn_a_success_into_a_failure(self):
        # Same policy as the log's: the ingest succeeded, and reporting a
        # failure against the one thing that did not fail would spend this
        # hook's single loud channel on the wrong event. The cost is that the
        # report says no success is recorded, which is true of the record.
        self.data_dir.mkdir(parents=True)
        self.record.mkdir()

        code, stderr, _ = self.run_hook(self.stop_payload())

        self.assertEqual(code, 0, stderr)
        self.assertEqual(stderr, "")


class HookToReportSeamTest(unittest.TestCase):
    """What the REPORT says about a database the hook built (#105).

    THE SEAM, and why #105 could ship green. Every class above asserts that the
    hook spawns the right `ingest.py` for the right file into the right
    database; `tests/test_serve.py` asserts what `/api/summary` says about a
    database. Both paths were covered. The join between them -- run the hook,
    then ask the report -- was covered by nothing, so the hook could succeed
    into a database the report then described as one no ingest had ever
    completed over, and no test on either side could see it.

    THIS CLASS IS THE ONLY ONE HERE THAT SPAWNS A PROCESS, and it is deliberate
    rather than an oversight of the module docstring's rule. An injected runner
    would assert the seam between the hook and a stand-in, which is the shape of
    coverage that let #105 through in the first place: the defect was in what
    `ingest.py --transcript` really wrote, and only the real child writes it.
    One spawn, one small transcript, no network, no real corpus.

    Same shape as #94, where every `serve` invocation in the skill was pinned
    and its `ingest` twin was not, and as #108, where a wrong scope was refused
    in one direction. Three defects, one shape: a rule applied to one member of
    a pair.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.data_dir = self.tmp / "plugin-data"
        # A structurally real transcript at the main-thread depth, hand-built:
        # `source_for_transcript()` classifies a single path by that layout, and
        # the usage values differ per class so a swapped column mapping could
        # not pass here either.
        projects = self.tmp / "projects" / "-synthetic-seam"
        projects.mkdir(parents=True)
        self.transcript = projects / "00000000-0000-4000-8000-000000000000.jsonl"
        self.transcript.write_text(
            json.dumps({
                "type": "assistant",
                "uuid": "u0",
                "sessionId": "00000000-0000-4000-8000-000000000000",
                "timestamp": "2026-08-07T00:00:00.000Z",
                "version": "2.1.223",
                "message": {
                    "id": "msg_seam",
                    "model": "claude-test-model",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "synthetic"}],
                    "usage": {
                        "input_tokens": 13,
                        "output_tokens": 17,
                        "cache_creation_input_tokens": 19,
                        "cache_read_input_tokens": 23,
                    },
                },
            }) + "\n",
            encoding="utf-8",
        )
        self.db = self.data_dir / hook.DB_FILENAME

    def fire_hook(self) -> int:
        """One real `Stop`, spawning the real `ingest.py`. Returns the exit code."""
        stderr = io.StringIO()
        code = hook.main(
            stdin=io.StringIO(json.dumps({
                "session_id": "00000000-0000-4000-8000-000000000000",
                "transcript_path": str(self.transcript),
                "cwd": str(self.tmp),
                "hook_event_name": "Stop",
            })),
            env={
                "PATH": os.environ.get("PATH", ""),
                "PYTHONDONTWRITEBYTECODE": "1",
                "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT),
                "CLAUDE_PLUGIN_DATA": str(self.data_dir),
            },
            stderr=stderr,
        )
        self.assertEqual(code, hook.EXIT_OK, stderr.getvalue())
        return code

    def summary(self, **install) -> dict:
        api = serve.Api(self.db, **install)
        try:
            return api.summary(*serve.day_bounds(None, None))
        finally:
            api.conn.close()

    def test_the_report_reads_the_success_the_hook_just_recorded(self):
        # THE #116 SEAM, and the reason it is asserted here rather than on
        # either side alone. The hook writes `cpb-hook-state.json` beside the
        # database and `serve.py` looks for it beside the database it was
        # handed; two constants, two modules, and nothing in either file forces
        # them to agree. A rename on one side would leave both suites green and
        # every install reporting hooks that never ran.
        self.fire_hook()
        hooks = self.summary(
            script=REPO_ROOT / "serve.py",
            env={"CLAUDE_PLUGIN_ROOT": str(REPO_ROOT)},
        )["hooks"]
        self.assertEqual(hooks["state"], serve.HOOKS_RECORDED)
        self.assertIsNotNone(hooks["last_success_at"])
        self.assertEqual(
            hooks["interpreter"],
            sys.executable,
            "the report names an interpreter the hook did not run under",
        )

    def test_a_hook_whose_ingest_failed_records_no_success_at_the_seam(self):
        # The true alarm beside the false one, at the same seam: a real failing
        # child, and the report must not read the install as one whose hooks
        # work. It cannot yet say they never have -- no install date is
        # readable from a checkout -- and says WHICH absence that is.
        self.fail_hook()
        hooks = self.summary(
            script=REPO_ROOT / "serve.py",
            env={"CLAUDE_PLUGIN_ROOT": str(REPO_ROOT)},
        )["hooks"]
        self.assertIsNone(hooks["last_success_at"])
        self.assertIsNone(hooks["state"])
        self.assertEqual(
            hooks["unknown_reason"], serve.HOOKS_UNKNOWN_NO_INSTALL_RECORD
        )

    def test_the_hook_ingests_and_the_report_calls_the_database_current(self):
        # THE regression, end to end. Before #105 every assertion up to
        # `calls` passed and the three after it failed -- a hook that ingested
        # 5 calls and exited 0, over a database the report then called one no
        # ingest had ever completed over.
        self.fire_hook()
        payload = self.summary()
        self.assertGreater(payload["calls"], 0, "the hook ingested nothing")
        ingest_block = payload["ingest"]
        self.assertIsNotNone(
            ingest_block["last_run_at"],
            "the hook succeeded and the report says no run ever completed",
        )
        self.assertIs(ingest_block["stale"], False)
        self.assertIsNone(ingest_block["stale_unknown_reason"])

    def test_the_report_does_not_claim_the_corpus_was_scanned(self):
        # The other half. The hook read one file; nothing swept the corpus, and
        # the report must not say otherwise while calling the database current.
        self.fire_hook()
        ingest_block = self.summary()["ingest"]
        self.assertIsNone(ingest_block["last_full_scan_at"])
        self.assertEqual(
            ingest_block["full_scan_unknown_reason"],
            serve.FULL_SCAN_UNKNOWN_NONE_RECORDED,
        )

    def test_the_health_band_agrees_with_the_report_it_sits_above(self):
        self.fire_hook()
        payload = self.summary()
        check = next(
            c
            for c in payload["health"]["checks"]
            if c["check"] == serve.CHECK_INGEST_AGE
        )
        self.assertEqual(check["state"], serve.HEALTH_OK)

    def fail_hook(self) -> None:
        """One real `Stop` whose spawned `ingest.py` FAILS, for the true alarm.

        Produced by a real failing child rather than by deleting a stamp
        afterwards: the transcript is made unreadable, so `parse_file()` raises
        and `ingest.py` exits non-zero having already created the database.
        That is the state a half-finished hook leaves, and it is the state the
        report must not read as a healthy one.
        """
        os.chmod(self.transcript, 0o000)
        self.addCleanup(os.chmod, self.transcript, 0o600)
        try:
            with self.transcript.open("rb"):
                pass
        except OSError:
            pass
        else:  # running as root: the mode is advisory and the child succeeds
            self.skipTest("this process can read a mode-000 file")

        stderr = io.StringIO()
        code = hook.main(
            stdin=io.StringIO(json.dumps({
                "transcript_path": str(self.transcript),
                "hook_event_name": "Stop",
            })),
            env={
                "PATH": os.environ.get("PATH", ""),
                "PYTHONDONTWRITEBYTECODE": "1",
                "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT),
                "CLAUDE_PLUGIN_DATA": str(self.data_dir),
            },
            stderr=stderr,
        )
        self.assertEqual(code, hook.EXIT_NONBLOCKING_ERROR, stderr.getvalue())
        self.assertTrue(self.db.is_file(), "the child never reached the database")

    def test_a_hook_whose_ingest_fails_leaves_the_age_unknown(self):
        # THE TRUE ALARM, pinned at the same seam as the false one, because the
        # cheap way to silence a false INCONCLUSIVE is to stop raising the real
        # one. A hook whose spawned ingest dies after the schema is stamped and
        # before the run is leaves a database that HOLDS the run table and no
        # row -- which is exactly what a database nothing has ever run over
        # holds, and the report must say "unknown age", not "fresh".
        self.fail_hook()
        ingest_block = self.summary()["ingest"]
        self.assertIsNone(ingest_block["last_run_at"])
        self.assertIsNone(
            ingest_block["stale"], "a failed ingest is not a fresh one"
        )
        self.assertEqual(
            ingest_block["stale_unknown_reason"],
            serve.STALE_UNKNOWN_NO_RUN_RECORDED,
        )


if __name__ == "__main__":
    unittest.main()
