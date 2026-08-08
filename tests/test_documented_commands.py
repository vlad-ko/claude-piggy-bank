"""Pin the command the documentation tells a user to type against what ships.

Why this exists
---------------

`README.md` told readers to type `/cpb`. A plugin's skills are namespaced by the
plugin, so the command a marketplace install actually answers to is
`/claude-piggy-bank:cpb`, and the front door named a command the tool does not
have ([#111](https://github.com/vlad-ko/claude-piggy-bank/issues/111)).

Nothing could have caught it. `tests/test_plugin_manifest.py` pins the skill
file, its frontmatter and its flags -- the *artifact* -- and no test read the
prose that describes it. That is this project's recurring defect at one more
seam: a rule applied to one member of a pair while nobody asks what its sibling
says (#94's `serve`/`ingest`, #105's directory/single-file, #108's two scopes).
Fixing the prose alone would rot again, so the fix is a tie: **one composed
command, derived from the shipped artifacts and asserted equal to every
invocation the documentation prints** -- the same shape as `serve.RANKED_BY`,
where the heading and the `ORDER BY` are free to disagree until something makes
them one string.

Where each half of the command comes from
-----------------------------------------

Neither half is a literal here. A literal would pass while the artifact moved
underneath it, which is the failure this file exists to stop.

* The **namespace** is `name` in `.claude-plugin/plugin.json`, read at test
  time. Documented: plugin skills "use a `plugin-name:skill-name` namespace"
  ([Skills § Where skills live](https://code.claude.com/docs/en/skills),
  checked 2026-08-07).
* The **segment** is the directory under `skills/`, read at test time.
  Documented: `my-plugin/skills/review/SKILL.md` -> `/my-plugin:review`
  ([Skills § How a skill gets its command
  name](https://code.claude.com/docs/en/skills#how-a-skill-gets-its-command-name),
  checked 2026-08-07). In a *plugin* skill the frontmatter `name` replaces the
  directory name in that last segment, so the two are potentially different
  answers to "what does the reader type". This file **refuses** where they
  disagree rather than picking one: a check that cannot see its answer must not
  report a clean one.

The bare segment is not an error the docs may make
--------------------------------------------------

The same reference says the bare `/review` "also invokes the skill unless
another command already uses that name". So `/cpb` is not *always* wrong -- it
is wrong on any machine where something else has claimed `cpb`, and CPB cannot
know which machine it is being read on. A documented instruction whose truth
depends on the reader's install is the project's own defect in prose form: a
claim with a narrower scope than the sentence carrying it. The namespaced form
resolves unconditionally, so the documentation writes that one and this file
requires it.

What counts as an invocation, and what does not
-----------------------------------------------

`skills/cpb/SKILL.md`, `commands/cpb.md`, `hooks/cpb_ingest_hook.py` and
`python3 cpb.py serve` all contain the segment and none of them is something a
user types into Claude Code. The discriminator is deliberately structural
rather than a guess at intent: a **command-shaped token** is a `/` that does not
continue a path, followed by an optional `<namespace>:`, followed by the
segment, and not followed by anything that would make it a longer path or word.
`CommandTokenDiscriminatorTest` pins that separation directly, on synthetic
strings, so the corpus passing is not the only evidence that the rule works.

Which documents are covered
---------------------------

`README.md`, every `docs/**/*.md`, and `CLAUDE.md` -- discovered by glob, so a
new document joins the pin by existing rather than by being remembered.

Two files are outside it, each because it was decided:

* `CONTRIBUTING.md` -- contributor prose, and outside the change set that added
  this file. It carries one bare mention at the time of writing. Listed here so
  the exclusion is a decision somebody can overturn, not a gap somebody
  inherits.
* `skills/cpb/SKILL.md` -- the skill's own prompt, which tells the user how to
  re-open the offer. It is a **shipped** path (see
  `.github/scripts/check_plugin_version_bump.py`), so correcting its one bare
  mention requires a version bump, which #111 explicitly left to the owner
  along with the naming question. It is the one user-facing bare mention this
  change did not fix, and it is reached only by somebody for whom the bare form
  has already resolved.
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST = REPO_ROOT / ".claude-plugin" / "plugin.json"
SKILLS_DIR = REPO_ROOT / "skills"

# Markers such as `<!--cpb:version-->4.0.0<!--/cpb:version-->` are machine-read
# anchors, not prose anybody types. They are removed before scanning so that
# the discriminator never has to special-case one.
HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)

# Characters that make a `/` part of a path rather than the start of a command,
# and that make the segment part of a longer word rather than the whole of it.
PATHISH = r"0-9A-Za-z_.\-"


def command_token_pattern(segment: str) -> re.Pattern[str]:
    """Match a command-shaped mention of `segment`, with or without a namespace.

    The two lookarounds are the whole discriminator. Without the first,
    `skills/cpb/SKILL.md` reads as the command `/cpb`; without the second,
    `hooks/cpb_ingest_hook.py` does.
    """
    return re.compile(
        rf"(?<![{PATHISH}/])"
        r"/"
        r"(?:(?P<namespace>[0-9A-Za-z_\-]+):)?"
        rf"(?P<segment>{re.escape(segment)})"
        rf"(?![{PATHISH}/:])"
    )


def shipped_plugin_name() -> str:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    name = manifest.get("name")
    if not isinstance(name, str) or not name:
        raise AssertionError(f"{MANIFEST} declares no usable `name`")
    return name


def shipped_skill_directory() -> Path:
    directories = sorted(p for p in SKILLS_DIR.iterdir() if p.is_dir())
    if len(directories) != 1:
        # More than one skill and this file cannot say which command the prose
        # is describing, so it refuses instead of pinning an arbitrary one.
        raise AssertionError(
            f"expected exactly one skill directory under {SKILLS_DIR}, "
            f"found {[p.name for p in directories]} -- widen this test "
            f"deliberately rather than letting it pick one"
        )
    return directories[0]


def frontmatter_name(skill_file: Path) -> str | None:
    text = skill_file.read_text(encoding="utf-8")
    match = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    if match is None:
        return None
    named = re.search(r"(?m)^name:\s*(\S+)\s*$", match.group(1))
    return named.group(1) if named else None


def documents() -> list[Path]:
    found = [REPO_ROOT / "README.md", REPO_ROOT / "CLAUDE.md"]
    found.extend(sorted((REPO_ROOT / "docs").rglob("*.md")))
    missing = [p for p in found if not p.is_file()]
    if missing:
        raise AssertionError(f"documents named but absent: {missing}")
    return found


class ComposedCommandTest(unittest.TestCase):
    """The command is composed from the artifacts, not stated twice."""

    def test_the_namespace_is_the_manifest_name(self):
        self.assertEqual(shipped_plugin_name(), "claude-piggy-bank")

    def test_exactly_one_skill_ships_and_it_names_the_segment(self):
        self.assertEqual(shipped_skill_directory().name, "cpb")

    def test_the_frontmatter_name_agrees_with_the_directory(self):
        # In a plugin skill the frontmatter `name` REPLACES the directory name
        # in the last segment of the command (Skills, How a skill gets its
        # command name, checked 2026-08-07). Where the two disagree there are
        # two answers to what a reader types, and composing from the directory
        # would silently pick the losing one -- so refuse.
        directory = shipped_skill_directory()
        declared = frontmatter_name(directory / "SKILL.md")
        self.assertIsNotNone(
            declared,
            "the skill states its invocation name in frontmatter rather than "
            "leaving it inferred from the directory",
        )
        self.assertEqual(
            declared,
            directory.name,
            "frontmatter `name` sets the command's last segment and the "
            "directory name is what this test composes from; a disagreement "
            "means the documented command cannot be derived",
        )

    def test_the_composed_command_is_the_namespaced_form(self):
        composed = f"/{shipped_plugin_name()}:{shipped_skill_directory().name}"
        self.assertEqual(composed, "/claude-piggy-bank:cpb")


class CommandTokenDiscriminatorTest(unittest.TestCase):
    """A file reference is not an invocation, and the rule says which is which.

    Asserted on synthetic strings rather than on the corpus, so that a corpus
    which happens to contain no path mention could not make this pass.
    """

    def setUp(self) -> None:
        self.pattern = command_token_pattern("cpb")

    def matches(self, text: str) -> list[str]:
        return [m.group(0) for m in self.pattern.finditer(text)]

    def test_a_bare_invocation_is_found(self):
        self.assertEqual(self.matches("run `/cpb` again"), ["/cpb"])

    def test_a_namespaced_invocation_is_found_whole(self):
        self.assertEqual(
            self.matches("type `/claude-piggy-bank:cpb` here"),
            ["/claude-piggy-bank:cpb"],
        )

    def test_a_wrongly_namespaced_invocation_is_found(self):
        # Found, so that the corpus test can reject it. A namespace that is not
        # the plugin's is as unreachable as no namespace at all.
        self.assertEqual(self.matches("`/piggy:cpb`"), ["/piggy:cpb"])

    def test_file_references_are_not_invocations(self):
        for reference in (
            "`skills/cpb/SKILL.md` is the skill file",
            "it was `commands/cpb.md` until #73",
            "each trigger runs `hooks/cpb_ingest_hook.py`",
            "`python3 cpb.py serve` from a checkout",
            "appended to `cpb-hook.log` beside the database",
            "the planner is `hooks/cpb_backfill_plan.py`",
            "…/plugins/cache/claude-piggy-bank/1.7.0/hooks/cpb_ingest_hook.py",
            "the `cpb` skill, `name: cpb`, reported as `Skills (1)  cpb`",
        ):
            with self.subTest(reference=reference):
                self.assertEqual(self.matches(reference), [])

    def test_the_version_marker_is_not_an_invocation(self):
        marked = "CPB is **<!--cpb:version-->4.0.0<!--/cpb:version-->** and"
        self.assertEqual(self.matches(HTML_COMMENT.sub("", marked)), [])


class DocumentedInvocationTest(unittest.TestCase):
    """Every invocation the documentation prints is the command that ships."""

    def setUp(self) -> None:
        self.composed = (
            f"/{shipped_plugin_name()}:{shipped_skill_directory().name}"
        )
        self.pattern = command_token_pattern(shipped_skill_directory().name)

    def invocations(self, path: Path) -> list[str]:
        text = HTML_COMMENT.sub("", path.read_text(encoding="utf-8"))
        return [m.group(0) for m in self.pattern.finditer(text)]

    def test_every_documented_invocation_is_the_composed_command(self):
        wrong: list[str] = []
        for path in documents():
            for found in self.invocations(path):
                if found != self.composed:
                    wrong.append(
                        f"{path.relative_to(REPO_ROOT)}: {found} "
                        f"(should be {self.composed})"
                    )
        self.assertEqual(
            wrong,
            [],
            "a plugin skill is namespaced by its plugin, so the bare segment "
            "resolves only while nothing else on the reader's machine claims "
            "it -- see #111:\n" + "\n".join(wrong),
        )

    def test_the_front_door_names_the_command_at_least_once(self):
        # Without this, renaming the plugin or the skill would empty the
        # haystack and the test above would pass over documentation that no
        # longer mentions the command at all.
        found = self.invocations(REPO_ROOT / "README.md")
        self.assertIn(
            self.composed,
            found,
            f"README.md never tells a reader to type {self.composed}",
        )

    def test_the_plugin_document_names_the_command(self):
        found = self.invocations(REPO_ROOT / "docs" / "plugin.md")
        self.assertIn(self.composed, found)

    def test_the_install_step_is_as_prominent_as_the_marketplace_step(self):
        # `/plugin marketplace add` registers the catalog and installs nothing;
        # a reader who stops there has a marketplace and no plugin, which is
        # how #111 was found. Both lines belong wherever either appears.
        marketplace_add = "/plugin marketplace add vlad-ko/claude-piggy-bank"
        install = "/plugin install claude-piggy-bank@claude-piggy-bank"
        for path in documents():
            text = path.read_text(encoding="utf-8")
            if marketplace_add not in text:
                continue
            with self.subTest(document=str(path.relative_to(REPO_ROOT))):
                self.assertIn(
                    install,
                    text,
                    f"{path.name} shows `{marketplace_add}` without the "
                    f"install line that actually installs anything",
                )


if __name__ == "__main__":
    unittest.main()
