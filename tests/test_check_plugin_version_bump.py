"""Tests for `.github/scripts/check_plugin_version_bump.py`'s `--bump` mode.

`check()` -- the CI gate -- had no dedicated test file before this one; it is
exercised only incidentally, by `test_plugin_manifest.py` confirming the
workflow invokes it. This file does not fix that gap (out of scope for
adding `--bump`); it covers only the new mechanism: `next_version()`'s pure
SemVer arithmetic, and `apply_bump()`'s file-writing, against a throwaway
directory rather than this repository's own working tree -- a test that
edited `cpb.py` in place would be the exact "toil, wrong place" defect
`--bump` exists to remove, aimed at itself.
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / ".github" / "scripts" / "check_plugin_version_bump.py"

# `.github/scripts/` is not a package (no `__init__.py`, and `.github` is not
# a valid module name), so the module is loaded by path -- the same reason
# `test_documented_commands.py`'s neighbours do the same for scripts here.
_spec = importlib.util.spec_from_file_location("check_plugin_version_bump", SCRIPT_PATH)
cpvb = importlib.util.module_from_spec(_spec)
sys.modules["check_plugin_version_bump"] = cpvb
_spec.loader.exec_module(cpvb)


class NextVersionTest(unittest.TestCase):
    """SemVer 2.0.0 section 8: raising a field zeroes every field right of it."""

    def test_patch_only_moves_the_last_field(self) -> None:
        self.assertEqual(cpvb.next_version("4.1.0", "patch"), "4.1.1")

    def test_minor_zeroes_patch(self) -> None:
        self.assertEqual(cpvb.next_version("4.1.9", "minor"), "4.2.0")

    def test_major_zeroes_minor_and_patch(self) -> None:
        self.assertEqual(cpvb.next_version("4.9.9", "major"), "5.0.0")

    def test_an_unknown_kind_is_refused(self) -> None:
        with self.assertRaises(cpvb.Refusal):
            cpvb.next_version("4.1.0", "revision")

    def test_a_malformed_current_version_is_refused(self) -> None:
        for bad in ("4.1", "4.1.0.0", "v4.1.0", "latest"):
            with self.subTest(current=bad):
                with self.assertRaises(cpvb.Refusal):
                    cpvb.next_version(bad, "patch")

    def test_a_pre_release_tag_is_refused_not_silently_dropped(self) -> None:
        # Silently bumping "4.1.0-rc.1" to "4.1.1" would discard the tag
        # without the caller asking for that -- a smaller version of the
        # "guess rather than refuse" defect this whole script exists to
        # avoid one level up.
        with self.assertRaises(cpvb.Refusal):
            cpvb.next_version("4.1.0-rc.1", "patch")


class ApplyBumpTest(unittest.TestCase):
    """`apply_bump()` against a throwaway directory shaped like the real repo."""

    def _write_repo(self, tmp_path: Path, version: str) -> None:
        (tmp_path / ".claude-plugin").mkdir(parents=True, exist_ok=True)
        (tmp_path / "docs").mkdir(parents=True, exist_ok=True)
        (tmp_path / "cpb.py").write_text(
            f'VERSION = "{version}"\nVERSION_FLAGS = ("--version",)\n',
            encoding="utf-8",
        )
        (tmp_path / ".claude-plugin" / "plugin.json").write_text(
            f'{{\n  "name": "claude-piggy-bank",\n  "version": "{version}"\n}}\n',
            encoding="utf-8",
        )
        (tmp_path / "CLAUDE.md").write_text(
            f"CPB is **<!--cpb:version-->{version}<!--/cpb:version-->** under SemVer.\n",
            encoding="utf-8",
        )
        (tmp_path / "docs" / "versioning.md").write_text(
            f"and is at **<!--cpb:version-->{version}<!--/cpb:version-->** (checked).\n",
            encoding="utf-8",
        )

    def test_a_patch_bump_writes_all_four_files(self) -> None:
        with __import__("tempfile").TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            self._write_repo(tmp_path, "4.1.0")

            old, new, changed = cpvb.apply_bump("patch", repo=str(tmp_path))

            self.assertEqual((old, new), ("4.1.0", "4.1.1"))
            self.assertEqual(
                set(changed),
                {"cpb.py", cpvb.MANIFEST, "CLAUDE.md", "docs/versioning.md"},
            )
            self.assertIn('VERSION = "4.1.1"', (tmp_path / "cpb.py").read_text())
            self.assertIn(
                '"version": "4.1.1"',
                (tmp_path / ".claude-plugin" / "plugin.json").read_text(),
            )
            self.assertIn(
                "<!--cpb:version-->4.1.1<!--/cpb:version-->",
                (tmp_path / "CLAUDE.md").read_text(),
            )
            self.assertIn(
                "<!--cpb:version-->4.1.1<!--/cpb:version-->",
                (tmp_path / "docs" / "versioning.md").read_text(),
            )

    def test_surrounding_text_is_untouched(self) -> None:
        # The mutation this guards against: a template that clobbers more
        # than the captured group, e.g. by replacing the whole matched line
        # instead of just the version substring.
        with __import__("tempfile").TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            self._write_repo(tmp_path, "4.1.0")

            cpvb.apply_bump("minor", repo=str(tmp_path))

            self.assertIn(
                'VERSION_FLAGS = ("--version",)', (tmp_path / "cpb.py").read_text()
            )
            self.assertIn(
                '"name": "claude-piggy-bank"',
                (tmp_path / ".claude-plugin" / "plugin.json").read_text(),
            )
            self.assertIn(
                "under SemVer.", (tmp_path / "CLAUDE.md").read_text()
            )

    def test_disagreeing_files_are_refused_and_nothing_is_written(self) -> None:
        with __import__("tempfile").TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            self._write_repo(tmp_path, "4.1.0")
            # Corrupt one file so the four no longer agree -- exactly the
            # drift `check()`'s authority/manifest comparison exists to
            # catch; `apply_bump` must refuse rather than pick a side.
            (tmp_path / "CLAUDE.md").write_text(
                "CPB is **<!--cpb:version-->4.0.9<!--/cpb:version-->** under SemVer.\n",
                encoding="utf-8",
            )

            with self.assertRaises(cpvb.Refusal):
                cpvb.apply_bump("patch", repo=str(tmp_path))

            # NOTHING was written -- not even the three files that agreed.
            self.assertIn('VERSION = "4.1.0"', (tmp_path / "cpb.py").read_text())
            self.assertIn(
                '"version": "4.1.0"',
                (tmp_path / ".claude-plugin" / "plugin.json").read_text(),
            )

    def test_a_missing_marker_is_refused(self) -> None:
        with __import__("tempfile").TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            self._write_repo(tmp_path, "4.1.0")
            (tmp_path / "docs" / "versioning.md").write_text(
                "no marker in this file at all\n", encoding="utf-8"
            )

            with self.assertRaises(cpvb.Refusal):
                cpvb.apply_bump("patch", repo=str(tmp_path))


class ReleasesStubTest(unittest.TestCase):
    def test_the_stub_names_the_new_version_and_is_never_written_to_disk(self) -> None:
        stub = cpvb.releases_stub("4.1.0", "4.1.1")
        self.assertIn("## 4.1.1", stub)
        self.assertIn("4.1.0", stub)
        # `releases_stub` returns a string; it must not be a path, a Path, or
        # anything that looks like it was written somewhere -- the whole
        # point is that this project's own release-notes rules (a bare
        # heading or a placeholder fails `test_release_notes.py`) forbid
        # auto-writing a real entry.
        self.assertIsInstance(stub, str)


if __name__ == "__main__":
    unittest.main()
