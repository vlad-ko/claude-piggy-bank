"""Tests for the Claude Code plugin packaging: manifest, marketplace, hooks, skill.

These files are configuration, not code, so nothing else in the suite would
notice if they broke. The failure mode is the quiet one: Claude Code skips a
plugin whose `hooks.json` will not parse or whose skill path does not resolve,
and the user's first symptom is a report that stopped updating -- which is
exactly the "absence rendered as a value" the project forbids.

Everything asserted here is checked against the plugin specification at
https://code.claude.com/docs/en/plugins-reference and
https://code.claude.com/docs/en/plugin-marketplaces (checked 2026-08-07):

- the manifest lives at `.claude-plugin/plugin.json`, the catalog beside it at
  `.claude-plugin/marketplace.json`, and no *component* directory belongs in
  that folder ("Common mistake" warning, Plugin structure overview);
- plugin hooks live at `hooks/hooks.json` in the plugin root (File locations
  reference);
- skills live at `skills/<name>/SKILL.md`; `commands/` is the legacy flat-file
  layout, which the File locations reference marks "Use `skills/` for new
  plugins";
- `name` is the only required manifest field and must be kebab-case;
- a marketplace entry needs `name` and `source`, and a relative `source`
  resolves against the marketplace root -- the directory containing
  `.claude-plugin/`, not `.claude-plugin/` itself;
- `${CLAUDE_PLUGIN_ROOT}` is the absolute path to the installed plugin
  directory, and is the only correct way to reach a bundled script, because
  hooks run in Claude Code's current directory -- the user's project.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST = REPO_ROOT / ".claude-plugin" / "plugin.json"
MARKETPLACE = REPO_ROOT / ".claude-plugin" / "marketplace.json"
HOOKS = REPO_ROOT / "hooks" / "hooks.json"
SKILL = REPO_ROOT / "skills" / "cpb" / "SKILL.md"
LEGACY_COMMAND_DIR = REPO_ROOT / "commands"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "tests.yml"
VERSION_CHECK = REPO_ROOT / ".github" / "scripts" / "check_plugin_version_bump.py"

# The three triggers this plugin ships, and why each one earns its place.
# SubagentStop is the load-bearing one: subagent transcripts are reaped, and a
# run whose transcript is gone is unmeasured spend, not zero.
EXPECTED_EVENTS = {"SessionEnd", "SubagentStop", "Stop"}

PLUGIN_ROOT_PLACEHOLDER = "${CLAUDE_PLUGIN_ROOT}"


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


class ManifestTest(unittest.TestCase):
    def setUp(self) -> None:
        self.assertTrue(MANIFEST.is_file(), f"{MANIFEST} is missing")
        self.manifest = load_json(MANIFEST)

    def test_the_manifest_is_a_json_object(self):
        self.assertIsInstance(self.manifest, dict)

    def test_name_is_present_and_kebab_case(self):
        name = self.manifest["name"]
        self.assertEqual(name, "claude-piggy-bank")
        self.assertRegex(name, r"^[a-z0-9]+(-[a-z0-9]+)*$")

    def test_version_is_explicit_and_semantic(self):
        # Omitting `version` makes Claude Code fall back to the git commit SHA,
        # so every commit reads as a new release. An explicit version is what
        # lets a user tell which build they are running.
        self.assertRegex(self.manifest["version"], r"^\d+\.\d+\.\d+$")

    def test_license_matches_the_shipped_LICENSE_file(self):
        license_text = (REPO_ROOT / "LICENSE").read_text(encoding="utf-8")
        self.assertEqual(self.manifest["license"], "MIT")
        self.assertIn("MIT License", license_text)

    def test_repository_points_at_this_project(self):
        self.assertEqual(
            self.manifest["repository"], "https://github.com/vlad-ko/claude-piggy-bank"
        )

    def test_description_is_present_and_says_what_the_plugin_does(self):
        self.assertGreater(len(self.manifest["description"]), 20)

    def test_no_component_directory_is_hidden_inside_dot_claude_plugin(self):
        # Documented "Common mistake": the two manifests belong in
        # .claude-plugin/ and nothing else does. A hooks/ or skills/ directory
        # placed there is silently never loaded.
        contents = sorted(p.name for p in MANIFEST.parent.iterdir())
        self.assertEqual(contents, ["marketplace.json", "plugin.json"])

    def test_no_dependency_manifest_is_introduced(self):
        # The stdlib-only CI job fails on the mere presence of any of these.
        # Asserting it here too means the rule is visible where the packaging
        # lives, not only in the workflow.
        for forbidden in ("pyproject.toml", "setup.py", "package.json", "Pipfile"):
            self.assertFalse(
                (REPO_ROOT / forbidden).exists(), f"{forbidden} contradicts stdlib-only"
            )
        self.assertEqual(list(REPO_ROOT.glob("requirements*.txt")), [])


class HooksConfigTest(unittest.TestCase):
    def setUp(self) -> None:
        self.assertTrue(HOOKS.is_file(), f"{HOOKS} is missing")
        self.config = load_json(HOOKS)
        self.hooks = self.config["hooks"]

    def test_exactly_the_three_documented_triggers_are_registered(self):
        self.assertEqual(set(self.hooks), EXPECTED_EVENTS)

    def test_every_event_maps_to_a_list_of_matcher_groups(self):
        for event, groups in self.hooks.items():
            with self.subTest(event):
                self.assertIsInstance(groups, list)
                self.assertTrue(groups)
                for group in groups:
                    self.assertIsInstance(group.get("hooks"), list)
                    self.assertTrue(group["hooks"])

    def test_no_matcher_is_set_so_every_occurrence_fires(self):
        # An omitted matcher means "match all". Stop takes no matcher at all;
        # SessionEnd would filter on exit reason and SubagentStop on agent
        # type, and filtering either would drop measurable spend on the floor.
        for event, groups in self.hooks.items():
            for group in groups:
                with self.subTest(event):
                    self.assertNotIn("matcher", group)

    def handlers(self):
        for event, groups in self.hooks.items():
            for group in groups:
                for handler in group["hooks"]:
                    yield event, handler

    def test_every_handler_is_a_bounded_command_hook(self):
        for event, handler in self.handlers():
            with self.subTest(event):
                self.assertEqual(handler["type"], "command")
                # The documented default is 600 seconds for command hooks. A
                # measurement tool holding a session for ten minutes is not a
                # tolerable failure mode, so every timeout here is explicit.
                self.assertIn("timeout", handler)
                self.assertIsInstance(handler["timeout"], int)
                self.assertGreater(handler["timeout"], 0)
                self.assertLessEqual(handler["timeout"], 60)

    def test_every_handler_uses_exec_form_so_paths_need_no_quoting(self):
        for event, handler in self.handlers():
            with self.subTest(event):
                self.assertIsInstance(handler.get("args"), list)

    def test_every_handler_invokes_the_shipped_ingest_hook_by_plugin_root(self):
        for event, handler in self.handlers():
            with self.subTest(event):
                script_args = [a for a in handler["args"] if a.endswith(".py")]
                self.assertEqual(len(script_args), 1, handler["args"])
                arg = script_args[0]
                self.assertTrue(
                    arg.startswith(PLUGIN_ROOT_PLACEHOLDER + "/"),
                    f"{arg!r} must be reached through {PLUGIN_ROOT_PLACEHOLDER}: hooks"
                    " run in the user's project directory, not the plugin root",
                )
                relative = arg[len(PLUGIN_ROOT_PLACEHOLDER) + 1 :]
                self.assertTrue(
                    (REPO_ROOT / relative).is_file(),
                    f"hooks.json points at {relative}, which is not in the plugin",
                )

    def test_no_handler_is_async(self):
        # Async hooks are not deduplicated across firings, so two overlapping
        # ingests of the same transcript could race for the SQLite write lock
        # with no back-pressure. Synchronous firing serialises them per
        # session; the handler is bounded so it cannot hang one.
        for event, handler in self.handlers():
            with self.subTest(event):
                self.assertNotIn("async", handler)
                self.assertNotIn("asyncRewake", handler)

    def test_no_hook_reaches_the_network(self):
        # "Nothing leaves the machine" applies to the plugin surface too: an
        # http hook would post the event JSON, which names the user's project
        # paths, to a URL.
        for event, handler in self.handlers():
            with self.subTest(event):
                self.assertNotIn(handler["type"], {"http", "prompt", "agent", "mcp_tool"})


class SkillTest(unittest.TestCase):
    """`/cpb` lives at `skills/cpb/SKILL.md`, the layout for new plugins.

    It was `commands/cpb.md`. The File locations reference marks `commands/`
    as "Skills as flat Markdown files. Use `skills/` for new plugins", so this
    is a migration to the documented layout rather than a rewrite: the prompt
    moved unchanged apart from the frontmatter `name`.
    """

    def setUp(self) -> None:
        self.assertTrue(SKILL.is_file(), f"{SKILL} is missing")
        self.text = SKILL.read_text(encoding="utf-8")
        match = re.match(r"^---\n(.*?)\n---\n", self.text, re.DOTALL)
        self.assertIsNotNone(match, "a skill file needs YAML frontmatter")
        self.frontmatter = match.group(1)

    def test_the_legacy_flat_command_layout_is_gone(self):
        # Not "the skill exists" -- both layouts loading at once would give the
        # plugin two `/cpb`s, and a half-finished migration reads as a working
        # one until someone edits the wrong copy.
        self.assertFalse(
            LEGACY_COMMAND_DIR.exists(),
            "commands/ is the legacy layout and the skill has moved out of it;"
            " a file left behind there would load as a second skill",
        )

    def test_it_has_a_description(self):
        self.assertIn("description:", self.frontmatter)

    def test_the_invocation_name_is_stated_rather_than_inferred(self):
        # In a PLUGIN skill the frontmatter `name` sets the last segment of the
        # command (Skills, "How a skill gets its command name", checked
        # 2026-08-07), so `/claude-piggy-bank:cpb` is written down here rather
        # than left to depend on the directory being called `cpb`.
        self.assertRegex(self.frontmatter, r"(?m)^name: cpb$")
        self.assertEqual(SKILL.parent.name, "cpb")

    def test_it_is_not_model_invocable(self):
        # Starting a server is a side effect with a port and a lifetime. The
        # user asks for it; Claude does not decide to.
        self.assertIn("disable-model-invocation: true", self.text)

    def test_it_reaches_serve_py_through_the_plugin_root(self):
        self.assertIn(f"{PLUGIN_ROOT_PLACEHOLDER}/serve.py", self.text)
        self.assertTrue((REPO_ROOT / "serve.py").is_file())

    def invocations(self, script: str) -> list[str]:
        return [
            line
            for line in self.text.splitlines()
            if script in line and "python3" in line
        ]

    def test_every_database_touching_invocation_names_its_database(self):
        # BOTH scripts, not just `serve.py` (#94). The serve half was pinned
        # from the start because `serve.py` reads only `--db` and never
        # `CPB_DB`, so an omitted flag opens an empty default that looks like a
        # measured result. The ingest half was NOT pinned, on the reasoning
        # that `ingest.py` does consult `CPB_DB` -- true, and irrelevant here,
        # because nothing sets `CPB_DB` in this session: the hook sets it only
        # for the child it spawns. A bare `python3 ${CLAUDE_PLUGIN_ROOT}/
        # ingest.py` therefore fell through to `ingest.py`'s own default,
        # `${CLAUDE_PLUGIN_ROOT}/db/usage.db`, and wrote the user's only copy
        # of their history into the directory every plugin update replaces --
        # while the report kept serving the empty one.
        #
        # One invocation type guarded and its twin not is how that survived, so
        # the rule now ranges over both. Checked per invocation rather than
        # once for the file, so a line that drops the flag cannot hide behind
        # another line that still has it.
        for script in ("serve.py", "ingest.py"):
            lines = self.invocations(script)
            self.assertTrue(lines, f"the skill must actually invoke {script}")
            for line in lines:
                with self.subTest(script=script, line=line.strip()):
                    self.assertIn("--db", line)

    def test_no_documented_invocation_can_write_into_the_plugin_root(self):
        # The durability rule, asserted at the one place a *documented command*
        # could walk around it. `${CLAUDE_PLUGIN_ROOT}` is replaced on every
        # plugin update; past Claude Code's transcript retention the database
        # is the only copy of the history. Every `--db` in this file must name
        # the persistent directory or a value the user chose themselves.
        values = re.findall(r'--db\s+"([^"]+)"', self.text)
        self.assertTrue(values, "no --db value found to check")
        for value in values:
            with self.subTest(value):
                self.assertNotIn(
                    "CLAUDE_PLUGIN_ROOT",
                    value,
                    "a database under the plugin root is deleted by the next "
                    "plugin update",
                )
                self.assertIn(value, ('${CLAUDE_PLUGIN_DATA}/usage.db', "$CPB_DB"))

    def test_no_documented_invocation_deletes_measurements(self):
        # The sibling of the `--db` rule, pinned before it is needed rather
        # than after. `--prune-missing` DELETES the rows for sources no longer
        # on disk, and past Claude Code's transcript retention those rows are
        # the only copy of that history -- so it is opt-in, chosen by a user
        # who has read what it does. A skill that offered it as a remedy would
        # be a model deciding to discard measurements on a user's behalf. The
        # same reasoning already guards `hooks/cpb_ingest_hook.py`; a rule
        # enforced for one invocation and not its sibling is how #94 got in.
        for line in self.invocations("ingest.py") + self.invocations("serve.py"):
            with self.subTest(line.strip()):
                self.assertNotIn("--prune-missing", line)
        self.assertNotIn("--prune-missing", self.text)

    def test_serve_and_ingest_are_pointed_at_the_same_database(self):
        # The symptom #94 opens with: the remedy offered for an empty report
        # did not fix it, because the file it filled was not the file being
        # read. Two commands that disagree about which database this is look
        # correct one line at a time.
        def databases(script):
            return {
                match.group(1)
                for line in self.invocations(script)
                if (match := re.search(r'--db\s+"([^"]+)"', line))
            }

        self.assertEqual(databases("serve.py"), databases("ingest.py"))


# Reserved marketplace names, from the "Reserved names" note in
# https://code.claude.com/docs/en/plugin-marketplaces#marketplace-schema
# (checked 2026-08-07). A marketplace registered under one of these stops
# loading and reports an untrusted source, so the list is worth carrying.
RESERVED_MARKETPLACE_NAMES = {
    "claude-code-marketplace",
    "claude-code-plugins",
    "claude-plugins-official",
    "claude-plugins-community",
    "claude-community",
    "anthropic-marketplace",
    "anthropic-plugins",
    "agent-skills",
    "anthropic-agent-skills",
    "knowledge-work-plugins",
    "life-sciences",
    "claude-for-legal",
    "claude-for-financial-services",
    "financial-services-plugins",
    "first-party-plugins",
    "healthcare",
}


class MarketplaceTest(unittest.TestCase):
    """CPB is its own marketplace: the repository is both catalog and plugin.

    `claude plugin validate .` does NOT catch the failure that matters here.
    Measured 2026-08-07 against Claude Code 2.1.223: with the entry's `source`
    pointed at `./plugins/claude-piggy-bank`, a directory that does not exist,
    the validator still printed `Validation passed`. Install then fails on the
    user's machine. These assertions are the only thing standing between that
    typo and a broken install.
    """

    def setUp(self) -> None:
        self.assertTrue(MARKETPLACE.is_file(), f"{MARKETPLACE} is missing")
        self.catalog = load_json(MARKETPLACE)
        self.manifest = load_json(MANIFEST)

    def test_the_catalog_is_a_json_object_with_the_required_fields(self):
        self.assertIsInstance(self.catalog, dict)
        for field in ("name", "owner", "plugins"):
            with self.subTest(field):
                self.assertIn(field, self.catalog)

    def test_the_marketplace_name_is_kebab_case_and_not_reserved(self):
        name = self.catalog["name"]
        self.assertRegex(name, r"^[a-z0-9]+(-[a-z0-9]+)*$")
        self.assertNotIn(name, RESERVED_MARKETPLACE_NAMES)

    def test_the_owner_names_someone(self):
        self.assertTrue(self.catalog["owner"].get("name"))

    def test_the_catalog_describes_itself(self):
        # `claude plugin validate` warns "No marketplace description provided",
        # and the description is what a user sees before installing.
        self.assertGreater(len(self.catalog.get("description", "")), 20)

    def test_exactly_one_plugin_is_catalogued_and_it_is_this_one(self):
        entries = self.catalog["plugins"]
        self.assertEqual(len(entries), 1, "CPB is a single-plugin repository")
        self.assertEqual(entries[0]["name"], self.manifest["name"])

    def test_the_entry_source_resolves_to_a_real_plugin(self):
        # The one `claude plugin validate` misses. A relative source resolves
        # against the marketplace ROOT -- the directory holding
        # `.claude-plugin/`, not `.claude-plugin/` itself -- so this walks the
        # same path Claude Code would and demands a manifest at the end of it.
        source = self.catalog["plugins"][0]["source"]
        self.assertIsInstance(
            source, str, "a github/url/npm source would fetch a second copy of "
            "this repository rather than using the one already cloned"
        )
        self.assertTrue(source.startswith("./"), f"{source!r} must start with ./")
        self.assertNotIn("..", source, "a source may not escape the marketplace root")
        resolved = (MARKETPLACE.parent.parent / source).resolve()
        self.assertTrue(resolved.is_dir(), f"{source!r} resolves to {resolved}")
        self.assertTrue(
            (resolved / ".claude-plugin" / "plugin.json").is_file(),
            f"{source!r} resolves to {resolved}, which holds no plugin manifest",
        )

    def test_the_entry_declares_no_version_of_its_own(self):
        # "Avoid setting `version` in both `plugin.json` and the marketplace
        # entry. Claude Code always uses the `plugin.json` value without
        # warning" (Plugin marketplaces, Version resolution, checked
        # 2026-08-07). Measured the same day: an entry saying 9.9.9 against a
        # manifest saying 1.6.0 validated with a warning and installed 1.6.0.
        # One copy of the version cannot disagree with itself.
        self.assertNotIn("version", self.catalog["plugins"][0])

    def test_the_entry_does_not_restate_the_plugins_description(self):
        # Measured 2026-08-07: with no description in the entry,
        # `claude plugin details` showed the one from plugin.json. A second
        # copy would be free to drift from the first.
        self.assertNotIn("description", self.catalog["plugins"][0])


class VersionBumpCheckTest(unittest.TestCase):
    """Run the CI version check against built repositories.

    Claude Code keys its plugin cache on `version` in `plugin.json`, so a
    merged change with an unbumped version reaches no existing install.
    Measured 2026-08-07 against Claude Code 2.1.223: a shipped `SKILL.md`
    change published with the version left at 1.7.0 updated the marketplace
    clone, answered `already at the latest version (1.7.0)` to
    `claude plugin update`, and never reached the installed copy.

    A rule with cases needs cases run against it, or "enforced" and "deleted"
    look the same from the outside.
    """

    @classmethod
    def setUpClass(cls):
        cls.env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")

    def build(self, base_files: dict[str, str], head_files: dict[str, str]) -> Path:
        """A two-commit repository: `main` at `base_files`, HEAD adds the rest."""
        tree = Path(tempfile.mkdtemp(prefix="cpb-version-check-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(tree, ignore_errors=True))

        def run(*args):
            subprocess.run(
                ["git", "-C", str(tree), *args],
                check=True,
                capture_output=True,
                text=True,
            )

        def write(files):
            for name, content in files.items():
                path = tree / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")

        run("init", "-q", "-b", "main")
        run("config", "user.email", "t@example.invalid")
        run("config", "user.name", "t")
        write(base_files)
        run("add", "-A")
        run("commit", "-qm", "base")
        run("branch", "base-marker")
        write(head_files)
        run("add", "-A")
        run("commit", "-qm", "head")
        return tree

    def check(self, tree: Path, base: str = "base-marker"):
        return subprocess.run(
            [sys.executable, str(VERSION_CHECK), "--base", base, "--repo", str(tree)],
            capture_output=True,
            text=True,
            env=self.env,
            timeout=60,
        )

    @staticmethod
    def manifest(version: str) -> str:
        return json.dumps({"name": "claude-piggy-bank", "version": version}) + "\n"

    @classmethod
    def states(cls, version: str) -> dict[str, str]:
        """Both places the version lives, moved together.

        `cpb.VERSION` is the authority and the manifest repeats it, so a
        fixture that moved only one would be testing a state the repository is
        not allowed to be in -- and the check refuses that state on purpose.
        The fixtures use versions deliberately unlike the shipped one, since
        pinning them to it would make this file need editing at every release.
        """
        return {
            ".claude-plugin/plugin.json": cls.manifest(version),
            "cpb.py": f'VERSION = "{version}"\n',
        }

    def test_a_shipped_change_without_a_bump_fails(self):
        # The defect the check exists for.
        tree = self.build(
            {**self.states("4.1.0"), "skills/cpb/SKILL.md": "old\n"},
            {"skills/cpb/SKILL.md": "new\n"},
        )
        result = self.check(tree)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("still 4.1.0", result.stdout)
        self.assertIn("skills/cpb/SKILL.md", result.stdout)

    def test_a_shipped_change_with_a_bump_passes(self):
        tree = self.build(
            {**self.states("4.1.0"), "skills/cpb/SKILL.md": "old\n"},
            {**self.states("4.2.0"), "skills/cpb/SKILL.md": "new\n"},
        )
        result = self.check(tree)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("4.1.0 -> 4.2.0", result.stdout)

    def test_a_prerelease_counts_as_moving_forward(self):
        tree = self.build(
            {**self.states("4.1.0"), "hooks/hooks.json": "{}\n"},
            {**self.states("4.2.0-rc.1"), "hooks/hooks.json": '{"hooks": {}}\n'},
        )
        self.assertEqual(self.check(tree).returncode, 0)

    def test_a_version_that_moves_backwards_fails(self):
        # "Differs" is not "newer". An install already on 4.2.0 would never see
        # 4.1.0, and a user reading the number would be told they downgraded.
        tree = self.build(
            {**self.states("4.2.0"), "index.html": "old\n"},
            {**self.states("4.1.0"), "index.html": "new\n"},
        )
        result = self.check(tree)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("backwards", result.stdout)

    def test_a_release_does_not_read_as_older_than_its_prerelease(self):
        tree = self.build(
            {**self.states("4.2.0-rc.1"), "index.html": "old\n"},
            {**self.states("4.2.0"), "index.html": "new\n"},
        )
        self.assertEqual(self.check(tree).returncode, 0)

    def test_a_docs_only_change_needs_no_bump(self):
        # The narrowness matters as much as the rule: a check that demanded a
        # release for a typo in the README is one people route around.
        tree = self.build(
            {**self.states("4.1.0"),
             "README.md": "old\n", "docs/plugin.md": "old\n",
             "tests/test_x.py": "old\n",
             ".claude-plugin/marketplace.json": "{}\n"},
            {"README.md": "new\n", "docs/plugin.md": "new\n",
             "tests/test_x.py": "new\n",
             ".claude-plugin/marketplace.json": '{"name": "x"}\n'},
        )
        result = self.check(tree)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("need not", result.stdout)

    def test_an_unclassifiable_path_is_refused_rather_than_ignored(self):
        # `bin/` is a real plugin component directory (File locations
        # reference). A check that silently ignored it would report a clean
        # result it had no basis for.
        tree = self.build(self.states("4.1.0"), {"bin/cpb": "#!/bin/sh\n"})
        result = self.check(tree)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("bin/cpb", result.stderr)
        self.assertIn("REFUSED", result.stderr)

    def test_a_manifest_that_disagrees_with_cpb_VERSION_is_refused(self):
        # `cpb.VERSION` is the source of truth and the manifest repeats it. A
        # release that moved one and not the other would ship two answers to
        # "which build produced this number", and this check would otherwise be
        # reasoning about the copy.
        tree = self.build(
            self.states("4.1.0"),
            {".claude-plugin/plugin.json": self.manifest("4.2.0"),
             "cpb.py": 'VERSION = "4.1.0"\n'},
        )
        result = self.check(tree)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("cpb.py", result.stderr)
        self.assertIn("two answers", result.stderr)

    def test_a_missing_authority_constant_is_refused(self):
        tree = self.build(
            self.states("4.1.0"),
            {"cpb.py": "# the constant went somewhere else\n"},
        )
        result = self.check(tree)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("source of truth", result.stderr)

    def test_a_manifest_that_declares_no_version_is_refused(self):
        # Omitting `version` is documented and legitimate -- every commit then
        # counts as a new version -- but it is the opposite of the strategy
        # this check enforces, so it must be re-decided rather than pass.
        tree = self.build(
            self.states("4.1.0"),
            {".claude-plugin/plugin.json": '{"name": "claude-piggy-bank"}\n'},
        )
        result = self.check(tree)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("declares no `version`", result.stderr)

    def test_a_version_that_cannot_be_ordered_is_refused(self):
        tree = self.build(
            self.states("4.1.0"),
            {".claude-plugin/plugin.json": self.manifest("v4.2")},
        )
        result = self.check(tree)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("MAJOR.MINOR.PATCH", result.stderr)

    def test_an_unresolvable_base_is_refused_rather_than_skipped(self):
        # A shallow clone or a missing `github.event.before` must not read as
        # "nothing changed".
        tree = self.build(self.states("4.1.0"), {"skills/cpb/SKILL.md": "new\n"})
        result = self.check(tree, base="0000000000000000000000000000000000000000")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("REFUSED", result.stderr)

    def test_the_repository_as_it_stands_is_classifiable(self):
        # Every path this branch touches must be on one side of the two tables,
        # so the check never refuses for a reason nobody looked at.
        merge_base = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "merge-base", "HEAD", "origin/main"],
            capture_output=True,
            text=True,
        )
        if merge_base.returncode != 0:
            self.skipTest("no origin/main to diff against")
        result = self.check(REPO_ROOT, base=merge_base.stdout.strip())
        self.assertNotIn("REFUSED", result.stderr, result.stderr)


class VersionCheckIsWiredIntoCITest(unittest.TestCase):
    """The script is only a check while CI runs it and the fan-in requires it."""

    def setUp(self) -> None:
        self.workflow = WORKFLOW.read_text(encoding="utf-8")

    def test_the_script_exists_where_the_workflow_looks_for_it(self):
        self.assertTrue(VERSION_CHECK.is_file(), f"{VERSION_CHECK} is missing")
        self.assertIn(
            ".github/scripts/check_plugin_version_bump.py", self.workflow
        )

    def test_the_required_fan_in_job_depends_on_it(self):
        # Branch protection requires only `suite`. A version job that `suite`
        # did not name could fail forever without blocking a merge.
        self.assertIn("needs: [test, stdlib-only, plugin-version]", self.workflow)
        self.assertIn("needs.plugin-version.result", self.workflow)

    def test_it_is_given_a_base_for_both_events_the_workflow_runs_on(self):
        # On a pull request the base is the branch being merged into; on a push
        # to main it is the previous tip. Neither may fall back to empty, which
        # the script would refuse -- correctly, but only after a red build
        # nobody could act on.
        self.assertIn("github.event.pull_request.base.sha", self.workflow)
        self.assertIn("github.event.before", self.workflow)


if __name__ == "__main__":
    unittest.main()
