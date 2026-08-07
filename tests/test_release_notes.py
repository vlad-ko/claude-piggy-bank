"""The release record cannot fall behind the version that ships (#101).

`docs/versioning.md` classifies a change as breaking -- exit statuses are inside
the governed CLI surface, which is what took CPB to 2.0.0 -- but classifying a
break is not announcing one. `docs/releases.md` is where a break is announced,
and this module is what stops it from being announced for the release *before*
the one a user installs.

The mechanism is deliberately the one that already worked. The version spans in
`README.md`, `CLAUDE.md` and `docs/versioning.md` are pinned to `cpb.VERSION` by
`DocsStateTheShippedVersionTest`, and that fixed a drift which had happened
twice. The same shape applies here: the repository is the source of truth, and
the record is test-pinned to the constant rather than to a release ritual
somebody has to remember. A record CI cannot see will drift, which is why
GitHub Releases alone was not enough -- nothing stops one being published there
as well, generated from this file.

WHAT IS PINNED, and why each half is needed:

  * the shipped `cpb.VERSION` has an entry in the record;
  * that entry has a BODY. An "an entry exists" assertion is cheapest to green
    by weakening what counts as an entry, so the shape is pinned too: a bare
    heading, a placeholder, or an emptied file all fail. `_record_problems()`
    is the whole of that judgment and this module tests it against synthetic
    records in both directions, because a checker that cannot fail proves
    nothing about the file it reads.

WHAT IS DELIBERATELY NOT PINNED: entries for historical versions.
`test_a_record_holding_only_the_current_entry_is_enough` asserts that
narrowness directly rather than leaving it to a comment. Requiring history
would force the record to be rewritten at every release, which is how a check
becomes something people route around -- the same reasoning that kept the
version-span pin to the current version only.
"""

from __future__ import annotations

import datetime
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cpb  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS = REPO_ROOT / "docs"
RECORD = DOCS / "releases.md"
DOCS_INDEX = DOCS / "README.md"

# Every `##` heading in the record is a release entry and nothing else is, so
# an entry cannot be lost behind a section the parser skips. A heading that
# does not match is a REFUSAL rather than a skip: silently ignoring an
# unparsable heading is how "2.O.O" (with letter O's) reads as no entry at all
# while looking, to a human, exactly like one.
ENTRY_HEADING = re.compile(
    r"^## (?P<version>\d+\.\d+\.\d+) — (?P<date>\d{4}-\d{2}-\d{2})$"
)
ANY_HEADING = re.compile(r"^#{1,6} ")

# An entry has to say what changed for a user. This floor is deliberately low
# and deliberately not zero: no test can judge whether prose is TRUE, so what
# it checks is that something is there -- enough to rule out a bare heading, a
# `TBD`, or a body that is only further headings. Roughly one line of prose.
MIN_ENTRY_BODY_CHARS = 80


class Entry:
    """One release entry: its heading fields and the lines beneath it."""

    def __init__(self, version: str, date: str, body: list[str]) -> None:
        self.version = version
        self.date = date
        self.body = body

    @property
    def body_text(self) -> str:
        """The entry's content, with blank lines and sub-headings removed.

        Sub-headings are excluded on purpose: `### Migration` with nothing
        under it is a heading with no content one level down, and it would
        otherwise buy an entry its way past the length floor.
        """
        return "\n".join(
            line.strip()
            for line in self.body
            if line.strip() and not ANY_HEADING.match(line)
        )


def parse_entries(text: str) -> tuple[list[Entry], list[str]]:
    """Split a record into entries, and report every `##` heading it refused.

    Returns `(entries, refusals)` rather than raising, so a malformed heading
    surfaces as a named problem beside the missing entry it causes.
    """
    entries: list[Entry] = []
    refusals: list[str] = []
    current: Entry | None = None
    for line in text.splitlines():
        if line.startswith("## "):
            match = ENTRY_HEADING.match(line)
            if match is None:
                refusals.append(line)
                current = None
                continue
            current = Entry(match["version"], match["date"], [])
            entries.append(current)
            continue
        if current is not None:
            current.body.append(line)
    return entries, refusals


def _record_problems(text: str, version: str) -> list[str]:
    """Every reason `text` fails to announce `version`. Empty means it does.

    This function is the whole of what "has an entry" means, which is why it is
    exercised against synthetic records below as well as the real one.
    """
    problems: list[str] = []
    entries, refusals = parse_entries(text)

    for heading in refusals:
        problems.append(
            f"heading is not a release entry and nothing else may be one: "
            f"{heading!r} (wanted `## <x.y.z> — <YYYY-MM-DD>`)"
        )

    if not entries:
        problems.append("the record holds no release entries at all")

    seen: dict[str, int] = {}
    for entry in entries:
        seen[entry.version] = seen.get(entry.version, 0) + 1
    for stated, count in seen.items():
        if count > 1:
            problems.append(
                f"{stated} has {count} entries; which one announces the "
                f"release is then ambiguous"
            )

    for entry in entries:
        try:
            datetime.date.fromisoformat(entry.date)
        except ValueError:
            problems.append(f"{entry.version} carries an impossible date {entry.date}")

    matching = [entry for entry in entries if entry.version == version]
    if not matching:
        problems.append(
            f"{version} ships and has no entry. Add one saying what changed "
            f"for a user, and -- if nothing did -- that there is nothing to "
            f"migrate."
        )
    for entry in matching:
        if len(entry.body_text) < MIN_ENTRY_BODY_CHARS:
            problems.append(
                f"{version}'s entry is a heading with "
                f"{len(entry.body_text)} characters of content under it; a "
                f"release record that says nothing is not a record"
            )
    return problems


class ReleaseRecordAnnouncesTheShippedVersionTest(unittest.TestCase):
    """The record read against the constant that actually ships."""

    def test_the_record_exists(self) -> None:
        # Named separately so deleting the file fails with the reason rather
        # than a FileNotFoundError inside an unrelated assertion.
        self.assertTrue(
            RECORD.is_file(),
            f"{RECORD} is gone. docs/versioning.md sends every reader of a "
            f"breaking change there.",
        )

    def test_the_shipped_version_is_announced(self) -> None:
        problems = _record_problems(RECORD.read_text(), cpb.VERSION)
        self.assertEqual(
            problems,
            [],
            "docs/releases.md does not announce the shipped release "
            f"({cpb.VERSION}): " + "; ".join(problems),
        )

    def test_the_record_is_indexed(self) -> None:
        # docs/README.md's own rule: a document not listed there is an orphan.
        self.assertIn(
            "releases.md",
            DOCS_INDEX.read_text(),
            "docs/README.md does not link the release record, which by that "
            "file's own rule makes it an orphan.",
        )

    def test_entries_run_newest_first(self) -> None:
        entries, _ = parse_entries(RECORD.read_text())
        order = [tuple(int(part) for part in e.version.split(".")) for e in entries]
        self.assertEqual(
            order,
            sorted(order, reverse=True),
            "the record claims to run newest first and does not; a reader "
            "looking for the release they just took should find it at the top.",
        )


class RecordProblemsHasTeethTest(unittest.TestCase):
    """The checker itself, against synthetic records.

    Without these, `_record_problems` returning `[]` unconditionally would
    leave every assertion above green -- the failure this file exists to
    prevent, one layer up.
    """

    GOOD = (
        "# Release record\n"
        "\n"
        "Intro prose that is not an entry.\n"
        "\n"
        "## 9.9.9 — 2026-08-07\n"
        "\n"
        "No break. Nothing to migrate: every command, flag, exit status,\n"
        "route and payload field behaves as it did.\n"
    )

    def test_a_record_holding_only_the_current_entry_is_enough(self) -> None:
        # THE NARROWNESS, asserted rather than commented. History is not
        # required: a record whose only entry is the shipped version passes.
        self.assertEqual(_record_problems(self.GOOD, "9.9.9"), [])

    def test_a_version_with_no_entry_fails(self) -> None:
        problems = _record_problems(self.GOOD, "9.10.0")
        self.assertTrue(problems)
        self.assertIn("9.10.0 ships and has no entry", "; ".join(problems))

    def test_an_emptied_record_fails(self) -> None:
        self.assertTrue(_record_problems("", "9.9.9"))
        self.assertTrue(_record_problems("# Release record\n\nProse.\n", "9.9.9"))

    def test_an_entry_that_is_only_a_heading_fails(self) -> None:
        text = "# Release record\n\n## 9.9.9 — 2026-08-07\n\n"
        problems = _record_problems(text, "9.9.9")
        self.assertTrue(problems)
        self.assertIn("heading with", "; ".join(problems))

    def test_a_body_of_only_sub_headings_fails(self) -> None:
        text = (
            "# Release record\n"
            "\n"
            "## 9.9.9 — 2026-08-07\n"
            "\n"
            "### What changed for a user, at considerable length and detail\n"
            "\n"
            "### Migration, also headed at length so the floor would be met\n"
        )
        self.assertTrue(_record_problems(text, "9.9.9"))

    def test_a_placeholder_body_fails(self) -> None:
        text = "# Release record\n\n## 9.9.9 — 2026-08-07\n\nTBD\n"
        self.assertTrue(_record_problems(text, "9.9.9"))

    def test_an_unparsable_heading_is_refused_not_skipped(self) -> None:
        text = "# Release record\n\n## 9.9.9\n\n" + "Prose. " * 20 + "\n"
        problems = _record_problems(text, "9.9.9")
        self.assertTrue(problems)
        self.assertIn("not a release entry", "; ".join(problems))

    def test_a_duplicated_version_fails(self) -> None:
        text = self.GOOD + "\n## 9.9.9 — 2026-08-06\n\n" + "Prose. " * 20 + "\n"
        problems = _record_problems(text, "9.9.9")
        self.assertTrue(problems)
        self.assertIn("2 entries", "; ".join(problems))

    def test_an_impossible_date_fails(self) -> None:
        text = self.GOOD.replace("2026-08-07", "2026-13-45")
        self.assertTrue(_record_problems(text, "9.9.9"))


if __name__ == "__main__":
    unittest.main()
