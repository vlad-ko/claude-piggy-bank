#!/usr/bin/env python3
"""Refuse a change to the shipped plugin that leaves `plugin.json`'s version alone.

Why this exists
---------------

Claude Code resolves a plugin's version from the first of: `version` in
`plugin.json`, `version` in the marketplace entry, the git commit SHA, then
`unknown` (documented: Plugins reference, Version management,
https://code.claude.com/docs/en/plugins-reference#version-management, checked
2026-08-07). CPB sets the first one, so **that string is the cache key** and
nothing else about a commit is visible to an installed user.

Measured here on 2026-08-07, against Claude Code 2.1.223, with a throwaway
marketplace served over smart HTTP into an isolated `CLAUDE_CONFIG_DIR`:

    plugin.json 1.6.0 -> 1.7.0, published:  `claude plugin update` moved the
        install to a new cache directory .../claude-piggy-bank/1.7.0/ and the
        session's hooks then ran from it.
    shipped SKILL.md changed, version left at 1.7.0, published:  the
        marketplace clone picked the change up, `claude plugin update`
        answered "already at the latest version (1.7.0)", and the installed
        copy never received the new line.

That second run is the whole reason for this file. A merged fix with an
unbumped version *looks* shipped and is not, and nothing in the repository
would have said so. This has been missed twice on this project already.

Why a PR check rather than a tag check
--------------------------------------

The obvious design -- fail a release whose version matches the previous tag --
checks a boundary this project's distribution path does not have.
`.claude-plugin/marketplace.json` lists the plugin with `"source": "./"`, so
the marketplace *is* the repository and the ref users resolve is the default
branch. **Every merge to `main` is a release**, with no tag in between. So the
check runs where it can still block: on the pull request, against the base
branch's version, before the change is user-visible.

What it refuses, and why refusal beats a guess
----------------------------------------------

The rule is "if the shipped plugin changed, the version must move forward", and
it is only as honest as its idea of what ships. A path this script cannot
classify is therefore a **refusal**, not a pass: a new `bin/` or `settings.json`
at the plugin root is a real plugin component, and a check that quietly ignored
it would report a clean result it had no basis for. Extending one of the two
tables below is a deliberate decision; inheriting a silent gap is not.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import PurePosixPath

MANIFEST = ".claude-plugin/plugin.json"

# `cpb.VERSION` is the project's source of truth for the version -- it is code,
# so it answers `cpb.py --version`, which is what a reader names when they say
# which build produced a figure. The manifest repeats it as a literal only
# because Claude Code's plugin loader reads JSON without running Python.
# `tests/test_cpb.py` pins the two equal; this script re-checks it because the
# check would otherwise be reasoning about a copy, and a release that bumped
# one and not the other would pass here while shipping two answers.
AUTHORITY = "cpb.py"
AUTHORITY_PATTERN = re.compile(r'^VERSION\s*=\s*"([^"]+)"', re.MULTILINE)

# Paths whose content reaches the installed plugin AND changes what it does.
# Directories are prefixes; `ROOT_PYTHON_SHIPS` covers every top-level module,
# because a new one is imported by the ones already here.
SHIPPED_DIRS = ("hooks/", "skills/", "commands/", "agents/", "vendor/")
SHIPPED_FILES = (MANIFEST, "index.html")
ROOT_PYTHON_SHIPS = True

# Paths that are copied into the plugin cache but change nothing an installed
# user runs. Each is listed because it was *decided*, not because it was
# missed.
#
#   marketplace.json  the catalog, not the plugin. A user's installed copy
#                     resolves its version from plugin.json whatever this says,
#                     and the catalog reaches them through
#                     `/plugin marketplace update`, which is a separate action.
#   tests, docs,      not executed or presented by the plugin.
#   .github, prose
NOT_SHIPPED_DIRS = ("tests/", "docs/", ".github/", "db/")
NOT_SHIPPED_FILES = (
    ".claude-plugin/marketplace.json",
    "README.md",
    "CLAUDE.md",
    "CONTRIBUTING.md",
    "LICENSE",
    ".gitignore",
)


class Refusal(Exception):
    """The check cannot see its answer, so it must not report a clean one."""


def git(*args: str, repo: str = ".") -> str:
    result = subprocess.run(
        ["git", "-C", repo, *args], capture_output=True, text=True
    )
    if result.returncode != 0:
        raise Refusal(
            f"git {' '.join(args)} failed: {result.stderr.strip() or 'no stderr'}"
        )
    return result.stdout


def classify(path: str) -> bool | None:
    """True if the path ships, False if it does not, None if unknown."""
    if path in SHIPPED_FILES:
        return True
    if path in NOT_SHIPPED_FILES:
        return False
    for prefix in SHIPPED_DIRS:
        if path.startswith(prefix):
            return True
    for prefix in NOT_SHIPPED_DIRS:
        if path.startswith(prefix):
            return False
    p = PurePosixPath(path)
    if ROOT_PYTHON_SHIPS and len(p.parts) == 1 and p.suffix == ".py":
        return True
    return None


def parse_version(raw: object, where: str) -> tuple:
    """SemVer precedence key. Refuses anything it cannot order."""
    if not isinstance(raw, str):
        raise Refusal(f"{where}: version is {type(raw).__name__}, not a string")
    core, _, pre = raw.partition("-")
    parts = core.split(".")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        raise Refusal(f"{where}: {raw!r} is not MAJOR.MINOR.PATCH")
    numbers = tuple(int(p) for p in parts)
    if not pre:
        # A release outranks every pre-release of the same core version
        # (SemVer 11.3), so it sorts above any identifier list.
        return numbers, (1, ())
    # Numeric identifiers compare numerically and rank below alphanumeric ones
    # (SemVer 11.4.1-2) -- so `rc.2` precedes `rc.10` rather than following it,
    # which a plain string compare would get backwards.
    identifiers = tuple(
        (0, int(part), "") if part.isdigit() else (1, 0, part)
        for part in pre.split(".")
    )
    return numbers, (0, identifiers)


def manifest_version_at(ref: str, repo: str) -> str | None:
    """The declared version at `ref`, or None if the manifest is not there."""
    result = subprocess.run(
        ["git", "-C", repo, "show", f"{ref}:{MANIFEST}"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise Refusal(f"{MANIFEST} at {ref} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise Refusal(f"{MANIFEST} at {ref} is not a JSON object")
    return data.get("version")


def authority_version_at(ref: str, repo: str) -> str | None:
    """`cpb.VERSION` at `ref`, read as text because `ref` is not checked out."""
    result = subprocess.run(
        ["git", "-C", repo, "show", f"{ref}:{AUTHORITY}"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    match = AUTHORITY_PATTERN.search(result.stdout)
    return match.group(1) if match else None


def check(base: str, repo: str = ".") -> list[str]:
    """Return the report lines. Raises `Refusal`, or `SystemExit(1)` on a fail."""
    lines: list[str] = []

    base_sha = git("rev-parse", "--verify", f"{base}^{{commit}}", repo=repo).strip()
    head_sha = git("rev-parse", "--verify", "HEAD^{commit}", repo=repo).strip()
    fork = git("merge-base", base_sha, head_sha, repo=repo).strip()

    head_version = manifest_version_at("HEAD", repo)
    if head_version is None:
        raise Refusal(
            f"{MANIFEST} at HEAD declares no `version`. Omitting it is a "
            "documented strategy -- every commit then counts as a new version "
            "-- but it is the opposite of the one this check enforces, so the "
            "check must be reconsidered rather than silently pass."
        )
    head_key = parse_version(head_version, f"{MANIFEST} at HEAD")

    authority = authority_version_at("HEAD", repo)
    if authority is None:
        raise Refusal(
            f"could not read `VERSION` from {AUTHORITY} at HEAD, so the "
            f"manifest's {head_version} cannot be confirmed against the "
            "constant that is the project's source of truth."
        )
    if authority != head_version:
        raise Refusal(
            f"{AUTHORITY} says VERSION = {authority!r} and {MANIFEST} says "
            f"{head_version!r}. They are one version stated twice; a release "
            "that moved one and not the other would ship two answers to 'which "
            "build produced this number'. Change the manifest to match "
            "cpb.VERSION, not the reverse."
        )
    lines.append(f"cpb.VERSION and the manifest agree at {authority}")

    base_version = manifest_version_at(fork, repo)
    if base_version is None:
        return [
            f"{MANIFEST} did not exist (or declared no version) at {fork[:12]}; "
            f"HEAD introduces {head_version}. Nothing to compare."
        ]
    base_key = parse_version(base_version, f"{MANIFEST} at {fork[:12]}")

    changed = [
        p
        for p in git("diff", "--name-only", fork, head_sha, repo=repo).splitlines()
        if p
    ]
    unknown = sorted(p for p in changed if classify(p) is None)
    if unknown:
        raise Refusal(
            "these changed paths are in neither table in this script, so "
            "whether they reach an installed user is unknown:\n  "
            + "\n  ".join(unknown)
            + "\nClassify each one in SHIPPED_* or NOT_SHIPPED_* and say why."
        )

    shipped = sorted(p for p in changed if classify(p))
    lines.append(f"base {fork[:12]} version {base_version}")
    lines.append(f"HEAD {head_sha[:12]} version {head_version}")
    lines.append(f"{len(changed)} changed path(s), {len(shipped)} of them shipped")

    if not shipped:
        lines.append(
            "Nothing an installed user runs changed, so the version need not "
            "move."
        )
        return lines

    if head_key == base_key:
        print("\n".join(lines))
        print(
            "\nThe shipped plugin changed but `version` is still "
            f"{head_version}. Claude Code keys its plugin cache on that "
            "string, so every existing install would keep the old copy and "
            "`/plugin update` would report 'already at the latest version'. "
            "The change would look shipped and would not be.\n"
            "\nShipped paths that changed:\n  " + "\n  ".join(shipped) + "\n"
            "\nBump `version` in .claude-plugin/plugin.json -- and `VERSION` "
            "in cpb.py, which tests/test_cpb.py pins equal to it, plus the "
            "three docs that state it. docs/versioning.md says which part to "
            "bump."
        )
        raise SystemExit(1)

    if head_key < base_key:
        print("\n".join(lines))
        print(
            f"\n`version` moved backwards, {base_version} -> {head_version}. "
            "An install already on the higher version would never see this "
            "one: the resolved version differs, but 'differs' is not 'newer' "
            "to a user reading the number."
        )
        raise SystemExit(1)

    lines.append(
        f"Shipped paths changed and the version moved forward "
        f"{base_version} -> {head_version}."
    )
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--base",
        required=True,
        help="the commit users are currently served: the PR's base, or the "
        "previous tip of main",
    )
    parser.add_argument("--repo", default=".", help="repository to inspect")
    args = parser.parse_args(argv)

    try:
        lines = check(args.base, args.repo)
    except Refusal as exc:
        # A refusal is not a pass with a warning. It exits the way a failure
        # does, because the one thing this must never do is report a clean
        # result it could not compute.
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
