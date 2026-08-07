"""Tests for the install-time backfill offer: the `/cpb` skill and its planner.

#97. CPB installs, its hooks start measuring, and the report is empty until the
user has worked for days -- while 2.10 GB and up to 61.5 days of transcripts sat
on the measured machine's disk the whole time, unread. `--all-projects` cures
that and nothing offered it. This is the offer.

Two halves, tested differently because they fail differently.

`hooks/cpb_backfill_plan.py` is code, so it is tested by running it and
asserting the state change -- above all the state change that must NOT happen:
it may never create a database, never write to one, and never ingest a
transcript. It exists so the estimate can be stated *before* the walk starts,
which is the only order in which "every project on this machine" can be a choice
rather than an assumption.

`skills/cpb/SKILL.md` is a PROMPT. Its instructions are what a model will
actually follow, so the parts of it that can be pinned are pinned here in the
same spirit as `tests/test_plugin_manifest.py`, which this file deliberately
imitates: every database-touching invocation names its database, nothing offers
`--prune-missing`, all three choices are present, the estimate is read from the
planner rather than written down, and "not now" carries a documented way back.

The rule this file most exists for is the one the offer could break most
quietly. "Every project on this machine" reads the directory names of every
project the user has, which are their own paths -- so it must be *asked*. A
prompt that phrased it as the recommended answer would be a model choosing that
on their behalf, and no amount of correct arithmetic downstream would make that
right.

Fixtures are synthetic and hand-built. No captured session content and no real
project name appears anywhere here.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import ingest  # noqa: E402

SKILL = REPO_ROOT / "skills" / "cpb" / "SKILL.md"
PLANNER = REPO_ROOT / "hooks" / "cpb_backfill_plan.py"


def load_planner():
    """Load the planner from its path in the plugin tree, as `test_plugin_hook`
    loads the ingest hook.

    Deliberately NOT `import cpb_backfill_plan`. `hooks/` is not on `sys.path`,
    and the `stdlib-only` CI job builds its set of local module names from the
    repository root and `tests/` only -- so a bare import of a module living
    under `hooks/` reads to that job as a third-party dependency and fails the
    build. Same reason, same idiom, one file over.
    """
    spec = importlib.util.spec_from_file_location("cpb_backfill_plan", PLANNER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cpb_backfill_plan = load_planner()

PLUGIN_ROOT_PLACEHOLDER = "${CLAUDE_PLUGIN_ROOT}"
PLUGIN_DATA_DB = "${CLAUDE_PLUGIN_DATA}/usage.db"

#: Every script the skill may invoke that touches the database. #94 was one of
#: these guarded and its sibling not; the planner is a third sibling, added by
#: this change, and it is on the list from its first day rather than after the
#: next incident.
DB_TOUCHING_SCRIPTS = ("serve.py", "ingest.py", "hooks/cpb_backfill_plan.py")

#: The three choices as the prompt must OFFER them: numbered, in order, in the
#: emphasis a model will read as the menu. Asserted as whole list items rather
#: than as loose phrases, because the first version of this test looked for the
#: words anywhere in the file and a deliberate mutation deleting choice 3 stayed
#: green -- "Not now" also appears in the prose explaining it. A choice that
#: survives only as prose is not a choice the user is given.
#:
#: The wording itself is the interface: "every project on this machine" names a
#: scope, and a paraphrase dropping "on this machine" would leave a model free
#: to present it as a bigger version of the same thing.
CHOICE_ITEMS = (
    "1. **This project only**",
    "2. **Every project on this machine**",
    "3. **Not now**",
)


def build_project(directory: Path, sessions: int = 1, subagents: int = 0) -> Path:
    """A synthetic project directory in Claude Code's layout.

    Hand-built, and deliberately not a copy of anything real: the token values
    differ per class so a swapped column mapping could not pass, and no path or
    prompt here came off a machine.
    """
    directory.mkdir(parents=True, exist_ok=True)
    for index in range(sessions):
        session = f"0000000{index}-0000-4000-8000-00000000000{index}"
        record = {
            "type": "assistant",
            "uuid": f"u{index}",
            "sessionId": session,
            "timestamp": "2026-08-07T00:00:00.000Z",
            "version": "2.1.223",
            "message": {
                "id": f"msg_{index}",
                "model": "claude-test-model",
                "role": "assistant",
                "content": [{"type": "text", "text": "synthetic"}],
                "usage": {
                    "input_tokens": 11,
                    "output_tokens": 22,
                    "cache_creation_input_tokens": 33,
                    "cache_read_input_tokens": 44,
                },
            },
        }
        (directory / f"{session}.jsonl").write_text(
            json.dumps(record) + "\n", encoding="utf-8"
        )
        for agent in range(subagents):
            agent_dir = directory / session / "subagents"
            agent_dir.mkdir(parents=True, exist_ok=True)
            (agent_dir / f"agent-{agent}.jsonl").write_text(
                json.dumps(record) + "\n", encoding="utf-8"
            )
    return directory


class PlannerBehaviourTest(unittest.TestCase):
    """Run the planner and assert what changed -- and what did not."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cpb-walkthrough-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = self.tmp / "projects"
        self.root.mkdir()
        self.db = self.tmp / "usage.db"
        self.project = build_project(self.root / "-synthetic-alpha", sessions=2)
        (self.root / "-synthetic-empty").mkdir()
        # A hermetic task index, so the walk never reads the real one under the
        # OS temp dir. `discover_task_index` of an empty directory is a scanned
        # zero, which is a different statement from never having looked.
        self.tasks = self.tmp / "tasks"
        self.tasks.mkdir()

    def run_planner(self, *args: str) -> str:
        """Capture stdout of one in-process run. Returns the printed text."""
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            status = cpb_backfill_plan.main(list(args))
        self.assertEqual(status, 0, buffer.getvalue())
        return buffer.getvalue()

    def verdict_of(self, text: str) -> str:
        match = re.search(r"(?m)^verdict: (\w+)", text)
        self.assertIsNotNone(match, f"no verdict line in:\n{text}")
        return match.group(1)

    # -- the state change that must not happen ---------------------------

    def test_planning_creates_no_database(self):
        # The whole point of a planner: it answers "what would this cost" and
        # leaves the disk as it found it. A file created here would also be a
        # file `serve.py` could open and report as an empty measured result.
        self.run_planner("--db", str(self.db), "--all-projects", str(self.root))
        self.assertFalse(
            self.db.exists(), "the planner created a database it was only asked to read"
        )

    def test_planning_ingests_nothing_from_an_existing_database(self):
        ingest.ingest(self.project, self.db, tasks_dir=self.tasks)
        before = self.db.read_bytes()
        self.run_planner("--db", str(self.db), "--all-projects", str(self.root))
        self.run_planner("--db", str(self.db), "--projects-dir", str(self.project))
        self.assertEqual(self.db.read_bytes(), before, "the planner wrote to the DB")

    def test_the_planner_never_calls_the_functions_that_do_the_work(self):
        # Structural, not incidental. `ingest()` and `backfill()` are the two
        # entry points that read transcripts and write rows; a planner that
        # could reach either is one refactor away from doing the walk it was
        # asked to describe -- with the scope consent still unasked.
        def forbidden(*args, **kwargs):
            raise AssertionError("the planner invoked the real ingest path")

        for name in ("ingest", "backfill"):
            with self.subTest(name):
                original = getattr(ingest, name)
                setattr(ingest, name, forbidden)
                self.addCleanup(setattr, ingest, name, original)
        self.run_planner("--db", str(self.db), "--all-projects", str(self.root))
        self.run_planner("--db", str(self.db), "--projects-dir", str(self.project))

    def test_the_database_is_required_and_never_defaulted(self):
        # #94: an omitted `--db` sent the user's only copy of their history into
        # the directory the next plugin update deletes. A planner that guessed a
        # path would report "nothing ingested yet" about a file nobody reads.
        with redirect_stderr(io.StringIO()) as complaint:
            with self.assertRaises(SystemExit) as raised:
                cpb_backfill_plan.main(["--projects-dir", str(self.project)])
        self.assertNotEqual(raised.exception.code, 0)
        self.assertIn("--db", complaint.getvalue())

    # -- the verdicts ----------------------------------------------------

    def test_an_absent_database_makes_the_corpus_pending(self):
        text = self.run_planner(
            "--db", str(self.db), "--all-projects", str(self.root)
        )
        self.assertEqual(self.verdict_of(text), cpb_backfill_plan.VERDICT_PENDING)
        self.assertIn("does not exist yet", text)

    def test_an_ingested_corpus_reads_up_to_date_rather_than_pending(self):
        # The offer must disappear once it has been taken. A verdict stuck on
        # PENDING would make `/cpb` ask forever, which is how a first-class
        # "not now" turns into nagging.
        ingest.ingest(self.project, self.db, tasks_dir=self.tasks)
        text = self.run_planner(
            "--db", str(self.db), "--projects-dir", str(self.project)
        )
        self.assertEqual(self.verdict_of(text), cpb_backfill_plan.VERDICT_UP_TO_DATE)

    def test_a_directory_holding_nothing_is_a_real_answer(self):
        text = self.run_planner(
            "--db", str(self.db), "--projects-dir", str(self.root / "-synthetic-empty")
        )
        self.assertEqual(
            self.verdict_of(text), cpb_backfill_plan.VERDICT_NO_TRANSCRIPTS
        )
        self.assertIn("not a failure", text)

    def test_an_empty_scope_is_quoted_no_estimate(self):
        # "~0s" is arithmetic over an empty set. It reads as a promise about a
        # walk that has nothing to walk, and this project does not render
        # absence as a value -- including as a duration.
        text = self.run_planner(
            "--db", str(self.db), "--projects-dir", str(self.root / "-synthetic-empty")
        )
        self.assertNotIn("estimate ~", text)

    def test_an_unreadable_database_is_unknown_and_never_pending(self):
        # The load-bearing distinction. `plan_backfill()` calls the whole corpus
        # pending when `ingest_state` cannot be read -- right for an ESTIMATE,
        # because nothing was shown to be skippable, and a lie as a FINDING,
        # because nobody established that any of it is missing.
        self.db.write_bytes(b"this is not a SQLite database")
        text = self.run_planner(
            "--db", str(self.db), "--all-projects", str(self.root)
        )
        self.assertEqual(self.verdict_of(text), cpb_backfill_plan.VERDICT_UNKNOWN)
        self.assertIn("unknown, not nothing", text)

    def test_the_root_of_every_project_is_not_one_empty_project(self):
        # #96's defect, one level up: `--projects-dir ~/.claude/projects` matches
        # neither glob. Reporting NO_TRANSCRIPTS here would answer "there is
        # nothing on this machine" to someone who named the wrong scope.
        text = self.run_planner("--db", str(self.db), "--projects-dir", str(self.root))
        self.assertEqual(self.verdict_of(text), cpb_backfill_plan.VERDICT_WRONG_SCOPE)
        self.assertIn("CHILDREN", text)

    # -- the estimate ----------------------------------------------------

    def test_the_estimate_is_the_walks_own_arithmetic(self):
        # Not a second formula that could drift from the run's. The planner
        # reads `BackfillPlan.estimated_seconds`, so the number offered and the
        # number the walk would quote are one arithmetic over one constant.
        text = self.run_planner(
            "--db", str(self.db), "--all-projects", str(self.root)
        )
        plan = ingest.plan_backfill(self.root, self.db)
        self.assertIn(
            f"estimate ~{ingest.human_seconds(plan.estimated_seconds)}", text
        )
        self.assertIn(ingest.INGEST_RATE_PROVENANCE, text)

    def test_the_estimate_is_not_a_second_rate_of_the_planners_own(self):
        # The fixture on disk is a few hundred bytes, so EVERY plausible rate
        # rounds to "0s" over it -- and a mutation replacing the plan's own
        # arithmetic with a per-megabyte rate stayed green until this test
        # existed. That is the fixture making the defect undetectable, which
        # this project's testing conventions forbid: where two quantities could
        # diverge, pin them deliberately unequal.
        #
        # 90 MB is ~13s at INGEST_BYTES_PER_SECOND and 1m30s at a 1 MB/s
        # invention, so the two cannot print the same string.
        pending = 90_000_000
        project = ingest.ProjectPlan(
            path=self.project,
            files=3,
            bytes=pending,
            pending_files=3,
            pending_bytes=pending,
            pending_known=True,
        )
        plan = ingest.BackfillPlan(
            root=self.root, projects=(project,), empty=(), unreadable=()
        )
        line = cpb_backfill_plan._estimate_lines(plan)[0]
        expected = ingest.human_seconds(pending / ingest.INGEST_BYTES_PER_SECOND)
        self.assertIn(f"estimate ~{expected}", line)
        self.assertNotEqual(expected, ingest.human_seconds(pending / 1_000_000))
        self.assertNotIn(ingest.human_seconds(pending / 1_000_000), line)

    def test_the_estimate_carries_its_provenance_rather_than_standing_alone(self):
        # A rate measured on ONE machine on one day is not a constant. It
        # crosses to the user with the sentence that says so, or it reads as a
        # promise about their disk.
        text = self.run_planner(
            "--db", str(self.db), "--all-projects", str(self.root)
        )
        self.assertIn("order of magnitude, not a promise", text)

    def test_directories_holding_nothing_are_counted_in_the_machine_wide_plan(self):
        # 63% of project directories on the machine measured for #97 held no
        # transcript. A plan silent about them is silent about most of what it
        # looked at.
        text = self.run_planner(
            "--db", str(self.db), "--all-projects", str(self.root)
        )
        self.assertIn("1 holds none", text)

    def test_interrupting_is_stated_as_safe(self):
        text = self.run_planner(
            "--db", str(self.db), "--all-projects", str(self.root)
        )
        self.assertIn("interrupting is safe", text)

    def test_it_says_plainly_that_it_did_nothing(self):
        # The reader is a model. "Here is a plan" and "here is what I just did"
        # are one careless sentence apart, and the second would report a
        # backfill that never ran.
        text = self.run_planner(
            "--db", str(self.db), "--all-projects", str(self.root)
        )
        self.assertIn(cpb_backfill_plan.NOTHING_HAPPENED, text)

    def test_every_verdict_has_a_note(self):
        # A verdict printed with no explanation is a token the model has to
        # interpret, and it will interpret a missing one fluently.
        verdicts = {
            value
            for name, value in vars(cpb_backfill_plan).items()
            if name.startswith("VERDICT_") and isinstance(value, str)
        }
        self.assertEqual(verdicts, set(cpb_backfill_plan.VERDICT_NOTES))


class PlannerIsRunnableAsShippedTest(unittest.TestCase):
    """It must work as a subprocess from an arbitrary cwd, which is how the
    skill runs it: hooks and skills run in the user's project, not the plugin
    root, so the `sys.path` bootstrap has to be self-locating."""

    def test_running_it_from_another_directory_finds_ingest(self):
        tmp = Path(tempfile.mkdtemp(prefix="cpb-walkthrough-cwd-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        result = subprocess.run(
            [sys.executable, "-B", str(PLANNER), "--db", str(tmp / "usage.db"),
             "--projects-dir", str(tmp / "absent")],
            cwd=str(tmp),
            capture_output=True,
            text=True,
            env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("verdict:", result.stdout)


class SkillOfferTest(unittest.TestCase):
    """The skill is a prompt; these are the parts of a prompt that can be pinned.

    Modelled on `tests/test_plugin_manifest.py::SkillTest`, which is read here
    rather than edited: that file owns the invariants the skill already had, and
    this one owns the ones the offer adds.
    """

    def setUp(self) -> None:
        self.assertTrue(SKILL.is_file(), f"{SKILL} is missing")
        self.text = SKILL.read_text(encoding="utf-8")

    def invocations(self, script: str) -> list[str]:
        return [
            line
            for line in self.text.splitlines()
            if script in line and "python3" in line
        ]

    def test_all_three_choices_are_offered(self):
        positions = []
        for item in CHOICE_ITEMS:
            with self.subTest(item):
                self.assertIn(item, self.text)
                positions.append(self.text.index(item))
        # In order, and in one run: three choices scattered through the file
        # would be three things a model might mention, not a menu it presents.
        self.assertEqual(positions, sorted(positions))

    def test_every_database_touching_invocation_names_its_database(self):
        # The #94 rule, extended to the third script rather than left to cover
        # the two that existed when it was written. A rule enforced for one
        # invocation and not its sibling is exactly how #94 got in.
        for script in DB_TOUCHING_SCRIPTS:
            lines = self.invocations(script)
            self.assertTrue(lines, f"the skill must actually invoke {script}")
            for line in lines:
                with self.subTest(script=script, line=line.strip()):
                    self.assertIn("--db", line)

    def test_every_named_database_is_the_durable_one(self):
        values = re.findall(r'--db\s+"([^"]+)"', self.text)
        self.assertTrue(values, "no --db value found to check")
        for value in values:
            with self.subTest(value):
                self.assertIn(value, (PLUGIN_DATA_DB, "$CPB_DB"))

    def test_the_planner_is_reached_through_the_plugin_root_and_exists(self):
        self.assertIn(
            f"{PLUGIN_ROOT_PLACEHOLDER}/hooks/cpb_backfill_plan.py", self.text
        )
        self.assertTrue(PLANNER.is_file())

    def test_nothing_in_the_offer_deletes_measurements(self):
        # `--prune-missing` past Claude Code's retention deletes the only
        # surviving copy of that history. A backfill offer is the last place it
        # belongs: the user is being asked to say yes to something, and the yes
        # must not be able to cover a deletion.
        self.assertNotIn("--prune-missing", self.text)
        self.assertNotIn(
            "--prune-missing", PLANNER.read_text(encoding="utf-8")
        )

    def test_every_project_is_never_the_default(self):
        # The rule this file most exists for. That scope reads the directory
        # names of every project on the machine -- the user's own paths -- so it
        # is theirs to choose. Pinned as a prohibition the prompt states in
        # words, because a model follows what the prompt says, not what a test
        # wishes it said.
        self.assertIn("Never pick choice 2 for the user", self.text)
        self.assertIn("recommended or\ndefault answer", self.text)
        self.assertIn("Wait for an answer.", self.text)

    def test_the_offer_is_asked_before_it_is_run(self):
        self.assertIn("ask, never assume", self.text)

    def test_not_now_carries_a_way_back(self):
        # A first-class third choice, not a deferral. If declining it left no
        # route back, "not now" would mean "never" for anyone who did not
        # remember a flag.
        section = self.text.split("If they choose 3")[1]
        self.assertIn("/cpb", section)
        self.assertIn("Do not ask a second time in this session.", section)

    def test_the_estimate_is_read_from_the_planner_not_written_down(self):
        # The tie between the prompt and the program, in the spirit of
        # `serve.RANKED_BY`: the phrase the skill tells a model to quote must be
        # a phrase the planner actually prints, or the instruction is to find
        # something that is not there -- and a model asked for a number it
        # cannot find will supply a plausible one.
        self.assertIn("estimate ~", self.text)
        self.assertIn("estimate ~", PLANNER.read_text(encoding="utf-8"))
        self.assertIn("that the step 1 output printed", self.text)
        self.assertIn("Never state a size or a duration this project did not", self.text)

    def test_the_skill_states_no_size_of_its_own(self):
        # There is no machine whose figures belong in this file. The one
        # measurement it does quote -- 12 of 19 directories -- is a count of
        # directories with its source named, not a size or a duration being
        # promised about the reader's disk.
        for unit in ("MB", "GB", "KB", "TB"):
            with self.subTest(unit):
                self.assertNotIn(unit, self.text)
        self.assertIsNone(
            re.search(r"~\s*\d", self.text),
            "a literal duration in the prompt is an estimate nobody measured",
        )

    def test_it_does_not_promise_the_report_will_be_useful(self):
        # A backfill clears the sample floors; it does not guarantee a verdict.
        # Overselling it turns "not enough data yet" -- the tool refusing to
        # invent a number -- into a broken promise.
        self.assertIn("Do not promise the report will now be useful", self.text)
        self.assertIn("minimum\nsample", self.text)

    def test_the_empty_state_is_reported_without_alarm(self):
        self.assertIn("new install, not a\nfault", self.text)

    def test_the_finding_includes_the_directories_that_held_nothing(self):
        self.assertIn("how many directories held no transcripts", self.text)
        self.assertIn("real answer", self.text)

    def test_interrupting_is_promised_safe_in_the_offer_itself(self):
        self.assertIn("safe to interrupt", self.text)

    def test_the_skill_does_not_summarise_figures(self):
        # Carried forward from the pre-#97 skill: `/cpb` may say where the
        # report is, never what it says.
        self.assertIn("The user reads the page.", self.text)


if __name__ == "__main__":
    unittest.main()
