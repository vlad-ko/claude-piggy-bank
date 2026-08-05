"""Tests for `cpb.py`, the single entry point (#21).

Two scripts you must `cd` to and run by filename are not a thing that lives in
a dev stack: `python3 ingest.py` names an implementation file, and which one
you get depends on the working directory. `cpb ingest` / `cpb serve` names the
tool and one command.

The entry point COMPOSES `ingest.py` and `serve.py` rather than replacing them:
each still owns its own argument parser, so the flags they document keep
working unchanged and there is exactly one definition of each. That makes two
properties load-bearing here and both are asserted below:

- everything after the subcommand is forwarded VERBATIM, including tokens that
  look like `cpb`'s own options (`cpb ingest --version` must reach `ingest.py`,
  not be answered by the wrapper);
- the underlying command's exit status is reproduced exactly. Both scripts
  refuse with `raise SystemExit("message")` -- an unpriced model, an empty
  `CPB_DB`, a missing database -- and a wrapper that turned any of those into
  exit 0, or dropped the message, would report a broken run as a good one.
  That is the same defect class the project forbids everywhere else: absence
  rendered as a value.
"""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cpb  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
ENTRY_POINT = REPO_ROOT / "cpb.py"
MANIFEST = REPO_ROOT / ".claude-plugin" / "plugin.json"


def stub_module(name: str, main) -> types.ModuleType:
    """A stand-in for `ingest`/`serve` carrying only the entry point's contract."""
    module = types.ModuleType(name)
    module.main = main  # type: ignore[attr-defined]
    return module


class RecordingMain:
    """Records the `sys.argv` its caller installed, then behaves as told."""

    def __init__(self, outcome=None):
        self.calls: list[list[str]] = []
        self.outcome = outcome

    def __call__(self) -> None:
        self.calls.append(list(sys.argv))
        if self.outcome is not None:
            raise self.outcome


def run(argv, ingest_main=None, serve_main=None):
    """Call `cpb.main(argv)` with the two shipped modules stubbed out.

    Patches `sys.modules` rather than `cpb`'s internals so the real import path
    is exercised: whatever the entry point does to reach `ingest.main`, it must
    reach it by module name.
    """
    modules = {}
    if ingest_main is not None:
        modules["ingest"] = stub_module("ingest", ingest_main)
    if serve_main is not None:
        modules["serve"] = stub_module("serve", serve_main)
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.dict(sys.modules, modules):
        with redirect_stdout(out), redirect_stderr(err):
            status = cpb.main(list(argv))
    return status, out.getvalue(), err.getvalue()


class SubcommandDispatchTest(unittest.TestCase):
    def test_ingest_dispatches_to_the_ingest_module(self):
        ingest_main = RecordingMain()
        status, _, _ = run(["ingest"], ingest_main=ingest_main)
        self.assertEqual(status, 0)
        self.assertEqual(len(ingest_main.calls), 1)

    def test_serve_dispatches_to_the_serve_module(self):
        serve_main = RecordingMain()
        status, _, _ = run(["serve"], serve_main=serve_main)
        self.assertEqual(status, 0)
        self.assertEqual(len(serve_main.calls), 1)

    def test_the_subcommand_does_not_reach_the_other_module(self):
        ingest_main, serve_main = RecordingMain(), RecordingMain()
        run(["serve"], ingest_main=ingest_main, serve_main=serve_main)
        self.assertEqual(ingest_main.calls, [])
        self.assertEqual(len(serve_main.calls), 1)

    def test_every_declared_command_names_a_shipped_module_with_a_main(self):
        # The dispatch table is the only place a command's module is named; if
        # it drifts from the files on disk the failure is an ImportError in
        # front of a user, not here.
        for name, command in cpb.COMMANDS.items():
            with self.subTest(name):
                self.assertTrue(
                    (REPO_ROOT / f"{command.module}.py").is_file(),
                    f"{name} -> {command.module}.py does not exist",
                )

    def test_the_ingest_and_serve_scripts_are_still_directly_runnable(self):
        # The entry point ADDS a front door; it does not close the old one.
        # Composition depends on both scripts keeping a zero-argument `main()`
        # that reads `sys.argv`, so pin that contract.
        for script in ("ingest.py", "serve.py"):
            with self.subTest(script):
                source = (REPO_ROOT / script).read_text(encoding="utf-8")
                self.assertIn("def main() -> None:", source)
                self.assertIn('if __name__ == "__main__":', source)


class ArgumentForwardingTest(unittest.TestCase):
    def test_remaining_arguments_are_forwarded_unchanged(self):
        ingest_main = RecordingMain()
        run(
            ["ingest", "--projects-dir", "somewhere", "--prune-missing"],
            ingest_main=ingest_main,
        )
        self.assertEqual(
            ingest_main.calls[0][1:],
            ["--projects-dir", "somewhere", "--prune-missing"],
        )

    def test_a_forwarded_argument_that_looks_like_our_own_option_is_not_eaten(self):
        # `--version` and `--help` belong to whichever parser is addressed.
        # After a subcommand that is the subcommand's, always.
        for flag in ("--version", "--help", "-h"):
            with self.subTest(flag):
                ingest_main = RecordingMain()
                run(["ingest", flag], ingest_main=ingest_main)
                self.assertEqual(ingest_main.calls[0][1:], [flag])

    def test_the_forwarded_program_name_names_the_subcommand(self):
        # argparse renders `sys.argv[0]` in the sub-parser's own usage line and
        # error messages. Left as `cpb.py` (or as the previous argv[0]) it
        # would tell the user to run something they did not type.
        ingest_main = RecordingMain()
        run(["ingest", "--prune-missing"], ingest_main=ingest_main)
        self.assertEqual(ingest_main.calls[0][0], "cpb ingest")

    def test_sys_argv_is_restored_after_a_successful_dispatch(self):
        before = list(sys.argv)
        run(["ingest"], ingest_main=RecordingMain())
        self.assertEqual(sys.argv, before)

    def test_sys_argv_is_restored_after_a_refusal(self):
        before = list(sys.argv)
        run(["ingest"], ingest_main=RecordingMain(SystemExit("refused")))
        self.assertEqual(sys.argv, before)

    def test_sys_argv_is_restored_after_an_unexpected_exception(self):
        before = list(sys.argv)
        with self.assertRaises(RuntimeError):
            run(["serve"], serve_main=RecordingMain(RuntimeError("boom")))
        self.assertEqual(sys.argv, before)


class ExitStatusTest(unittest.TestCase):
    def test_a_command_that_returns_normally_is_success(self):
        status, _, _ = run(["ingest"], ingest_main=RecordingMain())
        self.assertEqual(status, 0)

    def test_a_numeric_exit_status_is_reproduced(self):
        for code in (1, 2, 3, 42):
            with self.subTest(code):
                status, _, _ = run(
                    ["serve"], serve_main=RecordingMain(SystemExit(code))
                )
                self.assertEqual(status, code)

    def test_a_bare_sys_exit_is_success(self):
        # `sys.exit()` / `raise SystemExit` carries `code is None`, which the
        # interpreter treats as 0. Reproducing it as a failure would report a
        # command that finished as one that broke.
        status, _, _ = run(["ingest"], ingest_main=RecordingMain(SystemExit()))
        self.assertEqual(status, 0)

    def test_an_explicit_zero_exit_stays_zero(self):
        # `--help` inside a sub-parser exits 0 this way.
        status, _, _ = run(["ingest"], ingest_main=RecordingMain(SystemExit(0)))
        self.assertEqual(status, 0)

    def test_a_message_only_refusal_keeps_its_message_and_fails(self):
        # Both scripts refuse with `raise SystemExit("...")`. CPython prints
        # the message to stderr and exits 1; running under the entry point must
        # not lose either half.
        status, out, err = run(
            ["serve"],
            serve_main=RecordingMain(SystemExit("DB not found: nowhere.db")),
        )
        self.assertEqual(status, 1)
        self.assertIn("DB not found: nowhere.db", err)
        self.assertNotIn("DB not found", out)

    def test_an_unexpected_exception_is_not_swallowed(self):
        # A traceback is the honest outcome for a defect. Catching it to return
        # a status would report a crashed ingest as a completed one.
        with self.assertRaises(RuntimeError):
            run(["ingest"], ingest_main=RecordingMain(RuntimeError("boom")))


class UsageTest(unittest.TestCase):
    def test_no_command_exits_non_zero_with_usage(self):
        status, out, err = run([])
        self.assertNotEqual(status, 0)
        self.assertEqual(status, cpb.USAGE_EXIT_STATUS)
        self.assertIn("usage:", err)
        self.assertEqual(out, "")

    def test_an_unknown_command_exits_non_zero_and_names_it(self):
        status, out, err = run(["ingset"])
        self.assertEqual(status, cpb.USAGE_EXIT_STATUS)
        self.assertIn("ingset", err)
        self.assertIn("usage:", err)
        self.assertEqual(out, "")

    def test_the_usage_exit_status_is_the_conventional_one(self):
        self.assertEqual(cpb.USAGE_EXIT_STATUS, 2)

    def test_help_is_success_and_goes_to_stdout(self):
        for flag in ("-h", "--help"):
            with self.subTest(flag):
                status, out, err = run([flag])
                self.assertEqual(status, 0)
                self.assertIn("usage:", out)
                self.assertEqual(err, "")

    def test_usage_lists_every_command_it_can_dispatch(self):
        # A command reachable but undocumented is a command nobody runs.
        text = cpb.usage()
        for name, command in cpb.COMMANDS.items():
            with self.subTest(name):
                self.assertIn(name, text)
                self.assertIn(command.summary, text)


class VersionTest(unittest.TestCase):
    def test_version_flag_prints_the_version_and_exits_zero(self):
        status, out, err = run(["--version"])
        self.assertEqual(status, 0)
        self.assertEqual(out.strip(), f"cpb {cpb.VERSION}")
        self.assertEqual(err, "")

    def test_the_version_is_semantic(self):
        self.assertRegex(cpb.VERSION, r"^\d+\.\d+\.\d+$")

    def test_the_plugin_manifest_carries_the_same_version(self):
        # `cpb.VERSION` is the source of truth: it is code, so it answers
        # `--version` without reading a file that may not be there. The
        # manifest must repeat it as a literal because Claude Code's plugin
        # loader reads that JSON without running any Python. Two copies can
        # drift, so this test is the mechanism that forbids it -- if it fails,
        # change the manifest to match `cpb.VERSION`, not the reverse.
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        self.assertEqual(manifest["version"], cpb.VERSION)


DOCS = REPO_ROOT / "docs"
VERSIONING_RULE = DOCS / "versioning.md"
DOCS_INDEX = DOCS / "README.md"
README = REPO_ROOT / "README.md"
CONTRIBUTING = REPO_ROOT / "CONTRIBUTING.md"

# The code the versioning rule's central argument rests on. The rule says a
# SCHEMA_VERSION bump is not a MAJOR bump *because* these exist and bound what
# an upgrade is allowed to do to a database it did not write. If one is renamed
# or deleted, the rule is promising a guarantee the code no longer makes -- and
# a stale claim about compatibility is the same defect class as a stale number.
RULE_CITED_SYMBOLS = (
    "SCHEMA_VERSION",
    "IN_PLACE_UPGRADE_FROM",
    "IN_PLACE_CREATABLE_TABLES",
    "IN_PLACE_ADDABLE_COLUMNS",
    "PLAN_REFUSE_REAPED",
)


class VersioningRuleTest(unittest.TestCase):
    """#21/#55: the rule that says what a version number MEANS, and 1.0.0.

    Bumping the number without writing the rule is the weaker half: `1.0.0`
    with nothing saying what a major bump covers is a promise with no terms.
    So the number and its rule are pinned together here.

    The version assertion is deliberately `>= 1.0.0` rather than `== 1.0.0`.
    Pinning the literal would make every future release edit this file, which
    trains people to update the test to match the code -- the habit that makes
    a test decorative. What must not silently change is the DECISION: CPB has
    committed to a stable interface, and cannot slide back to a 0.x that
    promises nothing.
    """

    def parsed_version(self) -> tuple[int, ...]:
        return tuple(int(part) for part in cpb.VERSION.split("."))

    def test_the_version_has_reached_1_0_0(self):
        self.assertGreaterEqual(self.parsed_version(), (1, 0, 0))

    def test_the_versioning_rule_is_written_down(self):
        # The deliverable is the rule, not the digits.
        self.assertTrue(
            VERSIONING_RULE.is_file(), f"{VERSIONING_RULE.name} does not exist"
        )

    def test_the_rule_is_indexed_rather_than_orphaned(self):
        # `docs/README.md` states its own rule: a document not listed there is
        # an orphan. A compatibility promise nobody can find is not a promise.
        self.assertIn("versioning.md", DOCS_INDEX.read_text(encoding="utf-8"))

    def test_the_front_door_and_the_contributor_contract_both_point_at_it(self):
        # One home and a pointer: a user asking "what does 1.0.0 promise me?"
        # starts at the README, a contributor asking "which part do I bump?"
        # starts at CONTRIBUTING, and both must arrive at the same text rather
        # than at two copies free to drift.
        for path in (README, CONTRIBUTING):
            with self.subTest(path.name):
                self.assertIn("docs/versioning.md", path.read_text(encoding="utf-8"))

    def test_the_schema_argument_cites_code_that_exists(self):
        # The tie between the claim and the mechanism. The rule's hardest
        # clause -- that a schema bump is not a major bump -- is only true
        # while these bound the upgrade; nothing else would notice them going
        # away, because prose does not fail to import.
        import ingest

        rule = VERSIONING_RULE.read_text(encoding="utf-8")
        for symbol in RULE_CITED_SYMBOLS:
            with self.subTest(symbol):
                self.assertIn(symbol, rule, f"the rule no longer cites {symbol}")
                self.assertTrue(
                    hasattr(ingest, symbol),
                    f"the rule cites ingest.{symbol}, which does not exist",
                )

    def test_no_shipped_prose_still_calls_the_project_pre_1_0(self):
        # Both statements were true and are now false. A README that announces
        # a status the code contradicts is the documentation form of a number
        # that looks right.
        for path in (README, CONTRIBUTING):
            with self.subTest(path.name):
                self.assertNotIn("pre-1.0", path.read_text(encoding="utf-8"))


class EndToEndTest(unittest.TestCase):
    """Runs the real entry point in a subprocess, from an unrelated directory.

    The in-process tests stub both modules; these do not. They are the evidence
    that composition works against the scripts as actually shipped, and that
    the result does not depend on the working directory.
    """

    def cpb(self, *args, cwd=None):
        env = dict(os.environ)
        # `CPB_DB` would redirect the database these tests never want to touch,
        # and an empty one is a deliberate refusal in `resolve_db_path`.
        env.pop("CPB_DB", None)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        return subprocess.run(
            [sys.executable, str(ENTRY_POINT), *args],
            capture_output=True,
            text=True,
            cwd=cwd,
            env=env,
            timeout=60,
        )

    def test_version_answers_from_an_unrelated_working_directory(self):
        with tempfile.TemporaryDirectory() as elsewhere:
            result = self.cpb("--version", cwd=elsewhere)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), f"cpb {cpb.VERSION}")

    def test_no_command_is_a_usage_error_not_a_traceback(self):
        with tempfile.TemporaryDirectory() as elsewhere:
            result = self.cpb(cwd=elsewhere)
        self.assertEqual(result.returncode, cpb.USAGE_EXIT_STATUS)
        self.assertIn("usage:", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_an_unknown_command_is_a_usage_error_not_a_traceback(self):
        with tempfile.TemporaryDirectory() as elsewhere:
            result = self.cpb("srve", cwd=elsewhere)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("srve", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertNotIn("ModuleNotFoundError", result.stderr)

    def test_serve_refusal_reaches_the_caller_with_its_message(self):
        # The real `serve.py`, unedited: a database that is not there is a
        # refusal, not an empty report. Exit 1 and the message must survive the
        # wrapper.
        with tempfile.TemporaryDirectory() as elsewhere:
            missing = Path(elsewhere) / "not-a-database.db"
            result = self.cpb("serve", "--db", str(missing), cwd=elsewhere)
        self.assertEqual(result.returncode, 1)
        self.assertIn("DB not found", result.stderr)

    def test_ingest_rejects_a_flag_combination_through_the_wrapper(self):
        # The real `ingest.py`, unedited: `--transcript` with `--prune-missing`
        # is refused by ingest's own parser (exit 2). Proves the flags arrive
        # as typed and that argparse's status is not rewritten. Nothing is read
        # or written: the refusal precedes any file access.
        with tempfile.TemporaryDirectory() as elsewhere:
            result = self.cpb(
                "ingest",
                "--transcript",
                str(Path(elsewhere) / "session.jsonl"),
                "--prune-missing",
                cwd=elsewhere,
            )
        self.assertEqual(result.returncode, 2)
        self.assertIn("--prune-missing cannot be combined with --transcript", result.stderr)

    def test_a_subcommands_own_help_is_reachable_and_names_the_subcommand(self):
        with tempfile.TemporaryDirectory() as elsewhere:
            result = self.cpb("serve", "--help", cwd=elsewhere)
        self.assertEqual(result.returncode, 0)
        self.assertIn("--port", result.stdout)
        self.assertRegex(result.stdout, r"usage:\s+cpb serve")


class StdlibOnlyTest(unittest.TestCase):
    def test_the_entry_point_imports_only_the_standard_library_or_this_repo(self):
        # The CI job asserts this across every shipped module; asserting it for
        # the new entry point keeps the failure local and immediate.
        import ast

        local = {p.stem for p in REPO_ROOT.glob("*.py")}
        tree = ast.parse(ENTRY_POINT.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and not node.level:
                names = [(node.module or "").split(".")[0]]
            else:
                continue
            for name in names:
                with self.subTest(name):
                    self.assertTrue(
                        name in sys.stdlib_module_names or name in local,
                        f"{name} is neither stdlib nor local",
                    )


WORKFLOW = REPO_ROOT / ".github" / "workflows" / "tests.yml"
MANIFEST_STEP = "No manifest may declare a dependency"


def extract_ci_script(step_name: str) -> str:
    """Lift a step's `python - <<'PY' ... PY` heredoc out of the workflow.

    Reproduces what GitHub Actions hands bash: YAML strips the block scalar's
    common indentation, and a quoted heredoc delimiter passes the body through
    unchanged. Checked against a real YAML parser: the text this returns is
    byte-identical to the `run` value `yaml.load` yields for the same step.

    Raising is the point of the lookups below -- if the step is renamed or its
    shape changes, the tests that run it must fail rather than quietly stop
    testing anything.
    """
    lines = WORKFLOW.read_text(encoding="utf-8").splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.strip() == f"- name: {step_name}")
    run_at = next(i for i in range(start, len(lines)) if lines[i].strip() == "run: |")
    indent = len(lines[run_at]) - len(lines[run_at].lstrip()) + 2
    body: list[str] = []
    for line in lines[run_at + 1 :]:
        if line.strip() and not line.startswith(" " * indent):
            break
        body.append(line[indent:] if line.strip() else "")
    begin = next(i for i, ln in enumerate(body) if ln.startswith("python - <<'PY'"))
    end = next(i for i in range(begin + 1, len(body)) if body[i].rstrip() == "PY")
    return "\n".join(body[begin + 1 : end]) + "\n"


@unittest.skipUnless(
    sys.version_info >= (3, 11), "the CI check parses TOML with tomllib (3.11+)"
)
class ManifestCheckBehaviourTest(unittest.TestCase):
    """Run the narrowed `stdlib-only` manifest check against built trees (#21).

    The check used to reject a dependency manifest by FILENAME. That guarded a
    proxy: `pyproject.toml` failed even with `dependencies = []`, which
    declares exactly what the rule wants. It now tests the property -- a
    manifest that DECLARES a dependency fails, one that declares none does not
    -- and a rule with cases needs cases run against it, or "narrowed" and
    "disabled" look the same from the outside.

    Executes the step's script verbatim, so this cannot pass while CI runs
    something else.
    """

    @classmethod
    def setUpClass(cls):
        cls.script = Path(tempfile.mkdtemp()) / "manifest_check.py"
        cls.script.write_text(extract_ci_script(MANIFEST_STEP), encoding="utf-8")

    def check(self, files: dict[str, str]):
        with tempfile.TemporaryDirectory() as tree:
            for name, content in files.items():
                path = Path(tree) / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
            return subprocess.run(
                [sys.executable, str(self.script)],
                capture_output=True,
                text=True,
                cwd=tree,
                env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
                timeout=60,
            )

    def assertRejected(self, files: dict[str, str], because: str):
        result = self.check(files)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn(because, result.stdout)

    def assertAccepted(self, files: dict[str, str]):
        result = self.check(files)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_a_real_third_party_dependency_is_rejected(self):
        # The case the whole step exists for.
        self.assertRejected(
            {"pyproject.toml": '[project]\nname = "cpb"\ndependencies = ["requests>=2"]\n'},
            "requests>=2",
        )

    def test_optional_and_grouped_dependencies_are_rejected_too(self):
        self.assertRejected(
            {
                "pyproject.toml": '[project]\nname = "cpb"\n'
                '[project.optional-dependencies]\ndev = ["pytest"]\n'
            },
            "pytest",
        )
        self.assertRejected(
            {"pyproject.toml": '[project]\nname = "cpb"\n[dependency-groups]\nt = ["pytest"]\n'},
            "pytest",
        )
        self.assertRejected(
            {
                "pyproject.toml": '[tool.poetry]\nname = "cpb"\n'
                '[tool.poetry.dependencies]\npython = "^3.10"\nhttpx = "^0.27"\n'
            },
            "httpx",
        )

    def test_dependencies_declared_out_of_sight_are_rejected(self):
        # `dynamic` moves the declaration somewhere this cannot read. A check
        # that cannot see its answer must refuse, not report a clean one.
        self.assertRejected(
            {"pyproject.toml": '[project]\nname = "cpb"\ndynamic = ["dependencies"]\n'},
            "declared elsewhere",
        )

    def test_an_unparseable_manifest_is_rejected_rather_than_ignored(self):
        self.assertRejected({"pyproject.toml": "[project\n"}, "unreadable")

    def test_a_manifest_declaring_no_dependency_is_accepted(self):
        # The narrowing, in one case: this file was a build failure before #21
        # and declares exactly what the rule demands.
        self.assertAccepted(
            {"pyproject.toml": '[project]\nname = "cpb"\nversion = "0.1.0"\ndependencies = []\n'}
        )

    def test_a_build_backend_requirement_is_not_a_user_dependency(self):
        # A build frontend provisions `build-system.requires` in an isolated
        # environment it then discards; nothing reaches the environment the
        # user installs into.
        self.assertAccepted(
            {
                "pyproject.toml": '[project]\nname = "cpb"\nversion = "0.1.0"\n'
                '[build-system]\nrequires = ["setuptools>=68"]\n'
            }
        )

    def test_the_installer_formats_stay_rejected_on_sight(self):
        # Deliberate, not inherited: these exist only to drive an installer, so
        # a dependency-free one is a file with no purpose -- and the npm pair
        # implies a Node runtime the project rules out outright.
        for name, content in (
            ("requirements.txt", "\n"),
            ("requirements-dev.txt", "\n"),
            ("package.json", '{"dependencies": {}}\n'),
            ("Pipfile", "\n"),
            ("uv.lock", "\n"),
            ("setup.py", "from setuptools import setup\n"),
        ):
            with self.subTest(name):
                self.assertRejected({name: content}, "on sight")

    def test_a_manifest_hidden_in_a_subdirectory_is_found(self):
        # The old check ran `ls` in the repo root only.
        self.assertRejected({"tools/sub/requirements.txt": "flask\n"}, "requirements.txt")

    def test_the_plugins_own_json_is_not_mistaken_for_a_dependency_manifest(self):
        # `.claude-plugin/plugin.json` and `hooks/hooks.json` are configuration
        # this project ships. A rule that rejected them would fail every build.
        self.assertAccepted(
            {
                ".claude-plugin/plugin.json": MANIFEST.read_text(encoding="utf-8"),
                "hooks/hooks.json": (REPO_ROOT / "hooks" / "hooks.json").read_text(
                    encoding="utf-8"
                ),
            }
        )

    def test_a_tree_with_no_manifest_at_all_is_accepted(self):
        self.assertAccepted({"cpb.py": "print('hi')\n"})

    def test_the_repository_as_it_stands_passes(self):
        result = subprocess.run(
            [sys.executable, str(self.script)],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
            timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class WorkflowManifestRuleTest(unittest.TestCase):
    """The repo must stay on the accepted side of the narrowed rule."""

    def test_the_repos_own_json_files_are_not_named_like_dependency_manifests(self):
        banned = {
            "package.json",
            "package-lock.json",
            "Pipfile",
            "Pipfile.lock",
            "setup.py",
            "setup.cfg",
        }
        for path in REPO_ROOT.rglob("*.json"):
            if ".git" in path.parts:
                continue
            with self.subTest(str(path.relative_to(REPO_ROOT))):
                self.assertNotIn(path.name, banned)

    def test_no_dependency_declaring_manifest_is_introduced(self):
        # Narrowed with the CI check: `pyproject.toml` is permitted only if it
        # declares no dependencies, so its mere absence is no longer the rule.
        # These four remain rejected on sight -- see the workflow for why.
        for forbidden in ("setup.py", "setup.cfg", "package.json", "Pipfile"):
            with self.subTest(forbidden):
                self.assertFalse((REPO_ROOT / forbidden).exists())
        self.assertEqual(list(REPO_ROOT.glob("requirements*.txt")), [])

    def test_the_workflow_still_runs_a_manifest_check(self):
        # Deleting the step outright is a different decision from narrowing it,
        # and would leave every test above passing against a script CI no
        # longer runs.
        self.assertIn("tomllib", extract_ci_script(MANIFEST_STEP))


if __name__ == "__main__":
    unittest.main()


# Docs state the current version in prose, and twice running a release bumped
# `cpb.VERSION` and left them behind -- 1.0.0 -> 1.1.0, then 1.1.0 -> 1.2.0.
# Fixing them by hand each time is what produced the second one. A doc that
# names a version is making a claim about the build, so it is pinned to the
# build like any other claim.
#
# Only the CURRENT version is marked. `docs/versioning.md` cites 1.0.0, 4.0.0
# and a pre-release tag as worked examples and history, and those must stay
# free to differ -- a rule that pinned every version literal would force the
# history to be rewritten at every release, which is how a check becomes a
# thing people route around.
VERSION_MARK_OPEN = "<!--cpb:version-->"
VERSION_MARK_CLOSE = "<!--/cpb:version-->"

# Every file that states the current version. Listed, not globbed: a doc that
# stops stating it should fail loudly here rather than quietly drop out of the
# check.
FILES_STATING_THE_VERSION = (
    REPO_ROOT / "README.md",
    REPO_ROOT / "CLAUDE.md",
    DOCS / "versioning.md",
)


def _marked_versions(text: str) -> list[str]:
    out = []
    rest = text
    while VERSION_MARK_OPEN in rest:
        _, rest = rest.split(VERSION_MARK_OPEN, 1)
        if VERSION_MARK_CLOSE not in rest:
            # An unterminated mark is not "no version stated" -- it is a mark
            # someone half-removed. Refusing beats reading past it.
            raise AssertionError("unterminated cpb:version mark")
        value, rest = rest.split(VERSION_MARK_CLOSE, 1)
        out.append(value)
    return out


class DocsStateTheShippedVersionTest(unittest.TestCase):
    """The docs cannot drift from `cpb.VERSION` without going red."""

    def test_every_listed_doc_states_the_version_at_least_once(self) -> None:
        # Deleting the mark must fail rather than vacuously pass. Without this,
        # the cheapest way to make the next assertion green is to remove the
        # claim -- which is exactly the rot being prevented.
        for path in FILES_STATING_THE_VERSION:
            with self.subTest(doc=path.name):
                self.assertTrue(
                    _marked_versions(path.read_text()),
                    f"{path.name} no longer states the version; if that is "
                    f"deliberate, remove it from FILES_STATING_THE_VERSION and "
                    f"say why.",
                )

    def test_every_stated_version_is_the_one_that_ships(self) -> None:
        for path in FILES_STATING_THE_VERSION:
            for stated in _marked_versions(path.read_text()):
                with self.subTest(doc=path.name, stated=stated):
                    self.assertEqual(
                        stated,
                        cpb.VERSION,
                        f"{path.name} says {stated}; cpb.VERSION is "
                        f"{cpb.VERSION}. Bump the doc, not the constant.",
                    )

    def test_an_unmarked_version_elsewhere_is_not_pinned(self) -> None:
        # The rule is deliberately narrow, and this asserts the narrowness so a
        # later "tighten it" does not silently break the history in
        # versioning.md. Its worked examples cite versions that are not, and
        # must not become, the shipped one.
        rule = VERSIONING_RULE.read_text()
        unmarked = rule.replace(VERSION_MARK_OPEN, "").replace(
            VERSION_MARK_CLOSE, ""
        )
        others = {
            token
            for token in re.findall(r"\b\d+\.\d+\.\d+\b", unmarked)
            if token != cpb.VERSION
        }
        self.assertTrue(
            others,
            "versioning.md no longer cites any version other than the shipped "
            "one, so this test proves nothing -- check the worked examples "
            "survived.",
        )
