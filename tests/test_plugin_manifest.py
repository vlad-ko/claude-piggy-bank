"""Tests for the Claude Code plugin packaging: manifest, hooks, command.

These files are configuration, not code, so nothing else in the suite would
notice if they broke. The failure mode is the quiet one: Claude Code skips a
plugin whose `hooks.json` will not parse or whose command path does not
resolve, and the user's first symptom is a report that stopped updating --
which is exactly the "absence rendered as a value" the project forbids.

Everything asserted here is checked against the plugin specification at
https://code.claude.com/docs/en/plugins-reference (checked 2026-08-05):

- the manifest lives at `.claude-plugin/plugin.json` and only `plugin.json`
  belongs in that directory ("Common mistake" warning, Plugin structure
  overview);
- plugin hooks live at `hooks/hooks.json` in the plugin root (File locations
  reference);
- `name` is the only required manifest field and must be kebab-case;
- `${CLAUDE_PLUGIN_ROOT}` is the absolute path to the installed plugin
  directory, and is the only correct way to reach a bundled script, because
  hooks run in Claude Code's current directory -- the user's project.
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST = REPO_ROOT / ".claude-plugin" / "plugin.json"
HOOKS = REPO_ROOT / "hooks" / "hooks.json"
COMMAND = REPO_ROOT / "commands" / "cpb.md"

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
        # Documented "Common mistake": only plugin.json belongs in
        # .claude-plugin/. A hooks/ or commands/ directory placed there is
        # silently never loaded.
        contents = sorted(p.name for p in MANIFEST.parent.iterdir())
        self.assertEqual(contents, ["plugin.json"])

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


class CommandTest(unittest.TestCase):
    def setUp(self) -> None:
        self.assertTrue(COMMAND.is_file(), f"{COMMAND} is missing")
        self.text = COMMAND.read_text(encoding="utf-8")

    def test_it_has_yaml_frontmatter_with_a_description(self):
        match = re.match(r"^---\n(.*?)\n---\n", self.text, re.DOTALL)
        self.assertIsNotNone(match, "a command file needs YAML frontmatter")
        self.assertIn("description:", match.group(1))

    def test_it_is_not_model_invocable(self):
        # Starting a server is a side effect with a port and a lifetime. The
        # user asks for it; Claude does not decide to.
        self.assertIn("disable-model-invocation: true", self.text)

    def test_it_reaches_serve_py_through_the_plugin_root(self):
        self.assertIn(f"{PLUGIN_ROOT_PLACEHOLDER}/serve.py", self.text)
        self.assertTrue((REPO_ROOT / "serve.py").is_file())

    def test_every_serve_invocation_passes_the_database_explicitly(self):
        # serve.py reads only --db; unlike ingest.py it does not consult
        # CPB_DB. Relying on the environment here would silently open the
        # default database -- an empty or stale one -- while the hooks were
        # writing somewhere else entirely. Checked per invocation rather than
        # once for the file, so a command that drops the flag cannot hide
        # behind another line that still has it.
        invocations = [
            line
            for line in self.text.splitlines()
            if "serve.py" in line and "python3" in line
        ]
        self.assertTrue(invocations, "the command must actually start the server")
        for line in invocations:
            with self.subTest(line.strip()):
                self.assertIn("--db", line)


if __name__ == "__main__":
    unittest.main()
