#!/usr/bin/env python3
"""What a backfill would measure, measured -- without measuring any of it (#97).

CPB installs, its hooks start recording, and the report is empty until the user
has worked for days. That emptiness is self-inflicted: on the machine measured
for #97 there were 2.10 GB and up to 61.5 days of transcripts already on disk at
install time, and CPB read none of it. Claude Code reaps transcripts after
`cleanupPeriodDays`, so install is the moment of MAXIMUM available history and
every day without a backfill loses tail permanently.

`ingest.py --all-projects` is the cure and it exists. This file is what lets the
`/cpb` skill *offer* it.

Why a second entry point rather than a flag on the first
--------------------------------------------------------

`run_backfill_mode()` prints its plan and then walks, deliberately: five silent
minutes is indistinguishable from a hang. That order is right for someone who
has already typed the command, and unusable for an offer, which has to satisfy
two requirements at once that the combined form cannot:

- **The estimate is stated BEFORE anything starts.** Reading it off a walk that
  has started is not stating it beforehand.
- **"Every project on this machine" is never chosen on the user's behalf.** That
  scope reads directory names which are the user's own paths across their whole
  machine. Consent has to precede the walk, so the size the consent turns on has
  to precede it too.

So this module answers "what would it do" and can do nothing else. It opens the
database read-only through `ingest.tracked_state()`, never calls `ingest()` or
`backfill()`, and creates no file -- including the database itself, which is why
`--db` is REQUIRED rather than defaulted. #94 was a data-loss bug caused by an
omitted `--db`: the ingest half of the `/cpb` skill fell through to
`${CLAUDE_PLUGIN_ROOT}/db/usage.db`, which the next plugin update deletes. A
planner that quietly invented a database path could report "nothing ingested
yet" about a file nobody uses, which is a wrong answer wearing a measurement's
clothes.

Every figure here is arithmetic in `ingest.py` over `stat()` results. Nothing is
parsed, nothing is estimated twice, and nothing leaves the machine.

It lives beside the hook scripts because `hooks/` is where this plugin keeps the
executable code it ships that is not `ingest.py` or `serve.py`. It is NOT a
hook: no trigger in `hooks/hooks.json` names it, and it reads no event payload.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional

#: The repository root is this file's parent's parent -- the repository is the
#: plugin, so `ingest.py` sits one level up. Derived from `__file__` and not
#: from `${CLAUDE_PLUGIN_ROOT}`, so this is equally correct run straight from a
#: checkout.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import ingest  # noqa: E402  (must follow the sys.path insertion above)

#: The four things this planner can conclude. They are printed verbatim on a
#: `verdict:` line because the reader is a model following a prompt, and a
#: verdict it has to infer from three numbers is a verdict it can infer wrongly.
#:
#: UNKNOWN is not a fifth flavour of PENDING. When `ingest_state` cannot be
#: read, `plan_backfill()` calls the WHOLE corpus pending rather than guess --
#: correct for an estimate, and it would be a lie as a finding, because nobody
#: established that any of it is missing.
VERDICT_PENDING = "PENDING"
VERDICT_UP_TO_DATE = "UP_TO_DATE"
VERDICT_NO_TRANSCRIPTS = "NO_TRANSCRIPTS"
VERDICT_UNKNOWN = "UNKNOWN"
#: The fifth, and it exists for the same reason #96 needed five outcomes rather
#: than three: a directory whose CHILDREN are the projects is not a project that
#: held nothing. Reporting it as NO_TRANSCRIPTS would answer "there is nothing
#: on this machine" to someone who named the wrong scope -- which is #96's own
#: defect (`--projects-dir ~/.claude/projects` scanned zero files and reported
#: success) repeated one level up, in the offer built to cure it.
#:
#: IT COVERS BOTH DIRECTIONS SINCE #108, and it shipped covering one. A parent
#: named in project mode was caught; a PROJECT named in parent mode was not, and
#: measured on `main` at 3.2.0, 2026-08-07, a directory holding one transcript
#: reported `0 transcript file(s), 0 B on disk` and the note "that is a real
#: answer about the machine, not a failure". The closing sentence is what made
#: it worse than a bare zero: it told the reader the emptiness had been
#: ESTABLISHED, when nothing at that path had been examined.
#:
#: The pattern, which is why it is worth naming rather than patching: a rule
#: written for one member of a mode pair, with nobody asking what the sibling
#: does. #94 passed `--db` on every `serve` invocation and not on its `ingest`
#: twin; #105 stamped `ingest_runs` from full-scan mode and not from
#: single-file mode; #108 is the third. `BothScopesRefuseTheOthersShapeTest`
#: ranges over the pair rather than over one member of it.
VERDICT_WRONG_SCOPE = "WRONG_SCOPE"

VERDICT_NOTES = {
    VERDICT_PENDING: (
        "there are transcripts on disk in this scope that this database has"
        " not measured"
    ),
    VERDICT_UP_TO_DATE: (
        "every transcript on disk in this scope is already in this database;"
        " a backfill would read nothing new"
    ),
    VERDICT_NO_TRANSCRIPTS: (
        "this scope holds no transcripts at all. That is a real answer about"
        " the machine, not a failure"
    ),
    VERDICT_UNKNOWN: (
        "the database could not be read, so what it already holds is UNKNOWN"
        " rather than nothing; the size below covers the whole corpus because"
        " nothing could be shown to be skippable"
    ),
    # Direction-NEUTRAL since #108, deliberately. Two shapes reach this verdict
    # -- a parent named as a project and a project named as a parent -- and the
    # note is shared, so naming one of them here would state the wrong one half
    # the time. Which shape it is comes from the lines above, where it is
    # measured; what is common to both, and all this note may claim, is that
    # NOTHING was measured.
    VERDICT_WRONG_SCOPE: (
        "the directory named is not the shape this scope reads, and the lines"
        " above say which shape it is. Nothing was measured, so this says"
        " nothing about how much history is on this machine"
    ),
}

#: Printed last, always. The one sentence a reader must not get wrong about
#: this program: it is a measurement of what is on disk, not a run over it.
NOTHING_HAPPENED = (
    "nothing was ingested: this measured what is on disk and stopped."
)


def describe_database(db_path: Path) -> tuple[str, Optional[dict]]:
    """One line about the database, and the state `plan_backfill()` will use.

    Four outcomes, kept apart on purpose. "Does not exist" and "exists and
    tracks nothing" are both legitimate for a fresh install and both mean every
    file is pending, but only one of them is a database somebody has written to
    -- and "could not be read" is neither: it is the case where this program
    knows nothing, and it must say so rather than report a clean new install.
    """
    if not db_path.is_file():
        return (
            f"database: {db_path} does not exist yet"
            " (it is created by the first ingest)",
            {},
        )
    state = ingest.tracked_state(db_path)
    if state is None:
        return (
            f"database: {db_path} exists but could not be read, so what it"
            " already holds is unknown, not nothing",
            None,
        )
    if not state:
        return (
            f"database: {db_path} exists and tracks no source file yet",
            state,
        )
    return (
        f"database: {db_path} tracks {len(state)} source file(s)",
        state,
    )


def verdict_for(pending_files: int, files: int, pending_known: bool) -> str:
    """Which of `VERDICT_*` the counts support.

    Order is load-bearing. "No transcripts" is decided first because a scope
    with nothing in it is neither up to date nor pending -- both of those are
    claims about a corpus, and there is not one. `pending_known` is checked
    before `pending_files`, so an unreadable database can never be reported as
    a discovery about what is missing.
    """
    if not files:
        return VERDICT_NO_TRANSCRIPTS
    if not pending_known:
        return VERDICT_UNKNOWN
    return VERDICT_PENDING if pending_files else VERDICT_UP_TO_DATE


def _estimate_lines(plan: ingest.BackfillPlan) -> list[str]:
    """The estimate and the interrupt promise, in `ingest.py`'s own words.

    `estimated_seconds` is read off the plan rather than recomputed here. One
    arithmetic with two spellings is this project's recurring defect (see
    `RANKED_BY` in `serve.py`), and a planner whose number could drift from the
    walk's own would be quoting an estimate for a run that never happens.
    """
    return [
        f"  estimate ~{ingest.human_seconds(plan.estimated_seconds)}"
        f" ({ingest.INGEST_BYTES_PER_SECOND / 1_000_000:.2f} MB/s,"
        f" {ingest.INGEST_RATE_PROVENANCE})",
        "  interrupting is safe: every file that lands is committed, and a"
        " re-run skips it",
    ]


def plan_one_project(projects_dir: Path, db_path: Path) -> tuple[list[str], str]:
    """Size THIS project's own transcript directory. Returns lines and a verdict.

    Uses `ingest.project_sources()`, which is by construction exactly what one
    `ingest()` of this directory would scan -- so the size quoted here and the
    files the run reads cannot range over different sets.
    """
    lines, state = describe_database(db_path)
    out = [lines]
    classify = ingest.classify_projects_dir(projects_dir)
    if classify.outcome == ingest.PROJECTS_DIR_MISSING:
        # Absent, not empty -- but for THIS scope the two coincide in what they
        # justify: Claude Code creates the directory when it writes the project's
        # first transcript, so there is nothing to backfill either way. The
        # derivation is named because it is the one thing that could be wrong.
        out += [
            f"scanning {projects_dir} (this project only)",
            "  that directory does not exist. Claude Code creates it when it"
            " writes this project's first transcript, so there is nothing here"
            " to backfill.",
            "  the name is derived from this project's path; if you expected"
            " transcripts, pass the directory yourself with --projects-dir.",
        ]
        return out, VERDICT_NO_TRANSCRIPTS
    if classify.outcome == ingest.PROJECTS_DIR_UNREADABLE:
        out += [
            f"scanning {projects_dir} (this project only)",
            f"  that directory could NOT be read ({classify.reason}), so"
            " whether it holds transcripts is unknown rather than no.",
        ]
        return out, VERDICT_UNKNOWN
    if classify.outcome == ingest.PROJECTS_DIR_PARENT:
        # #96 exactly: this path's CHILDREN are the projects. Saying so beats
        # sizing zero files and calling the project empty.
        out += [
            f"scanning {projects_dir} (this project only)",
            "  that directory holds no transcripts of its own -- its CHILDREN"
            " are project directories. It is the root of every project on this"
            " machine, which is the other choice, not this one.",
        ]
        return out, VERDICT_WRONG_SCOPE

    sources, _ = ingest.project_sources(projects_dir)
    project = ingest._size_project(projects_dir, sources, state)
    # A one-project plan, built only so the estimate is the SAME arithmetic over
    # the same constant the machine-wide walk uses. Nothing below prints a
    # directory count, so `root` here is what is being scanned and not a claim
    # that this directory has project directories under it.
    plan = ingest.BackfillPlan(
        root=projects_dir, projects=(project,), empty=(), unreadable=()
    )
    out.append(f"scanning {projects_dir} (this project only)")
    out.append(
        f"  {project.files} transcript file(s),"
        f" {ingest.human_bytes(project.bytes)} on disk"
    )
    if not project.pending_known:
        out.append(
            "  the database could not be read, so NOTHING is known to be"
            " skippable -- the estimate below covers the whole corpus"
        )
    elif project.pending_files != project.files:
        out.append(
            f"  {project.pending_files} file(s) new or changed"
            f" ({ingest.human_bytes(project.pending_bytes)});"
            f" {project.files - project.pending_files} already ingested and"
            " will be skipped"
        )
    # No estimate for a scope with nothing in it. "~0s" is arithmetic over an
    # empty set and reads as a promise about a walk that has nothing to walk;
    # the verdict below is the whole answer in that case.
    if project.files:
        out += _estimate_lines(plan)
    return out, verdict_for(
        project.pending_files, project.files, project.pending_known
    )


def plan_every_project(root: Path, db_path: Path) -> tuple[list[str], str]:
    """Size every project under `root`, reusing `ingest.format_backfill_plan()`.

    The formatter is borrowed rather than reimplemented because it already says
    the four things in the order #97 asked for -- including how many directories
    held NOTHING, which was 12 of 19 on the measured machine. A walk silent
    about 63% of what it looked at leaves the reader unable to tell "there is
    nothing there" from "it did not look".

    **`root` is classified before it is walked (#108).** `plan_backfill()`
    ranges over `root`'s CHILDREN, so a `root` that is itself a project -- its
    transcripts at its own top level -- produced zero projects, zero files and
    the confident note "this scope holds no transcripts at all. That is a real
    answer about the machine, not a failure". It was not: the emptiness was
    assumed, not established, and the confident sentence is what made it worse
    than a bare zero. `plan_one_project()` has refused the mirror-image mistake
    since #97 shipped; this is the same refusal for the other member of the
    pair.

    A genuinely empty root still gets its confident zero. The classification
    below fires on PROJECT and on nothing else, so a root whose children were
    all examined and all held nothing is unchanged -- fixing a false zero by
    making every zero hedge would trade one unreadable answer for another.
    """
    line, _ = describe_database(db_path)
    classify = ingest.classify_projects_dir(root)
    if classify.outcome == ingest.PROJECTS_DIR_PROJECT:
        return (
            [
                line,
                f"scanning {root} (every project on this machine)",
                "  that directory IS one project: its transcripts sit at its"
                " own top level, and this scope reads the project directories"
                " UNDER a root. Nothing beneath it was measured, and nothing"
                " here says how much history is on this machine.",
                "  it is the other choice, not this one -- pass it as"
                f" --projects-dir {root} to size the project it is.",
            ],
            VERDICT_WRONG_SCOPE,
        )
    plan = ingest.plan_backfill(root, db_path)
    return (
        [line, ingest.format_backfill_plan(plan)],
        verdict_for(plan.pending_files, plan.files, plan.pending_known),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Measure what a backfill WOULD read, and ingest nothing. Prints a"
            " size, a time estimate and a verdict, so the /cpb skill can state"
            " the cost of a backfill before offering to run one (#97)."
        ),
    )
    parser.add_argument(
        "--db",
        type=Path,
        required=True,
        help=(
            "Database to compare the transcripts on disk against. REQUIRED and"
            " never defaulted: a planner that invented a path would report"
            " 'nothing ingested yet' about a file nobody reads (#94). It is"
            " opened read-only and never created."
        ),
    )
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument(
        "--projects-dir",
        type=Path,
        default=None,
        help=(
            "Plan ONE project's transcript directory. Defaults to this"
            " project's own, derived from the working directory the same way"
            " ingest.py derives it."
        ),
    )
    scope.add_argument(
        "--all-projects",
        nargs="?",
        type=Path,
        default=None,
        const=ingest.ALL_PROJECTS_DEFAULT_ROOT,
        metavar="ROOT",
        help=(
            "Plan EVERY project under ROOT, which defaults to"
            " ~/.claude/projects. Ask the user before using this: it reads the"
            " directory names of every project on the machine, which are their"
            " own paths."
        ),
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    db_path = args.db.expanduser()

    if args.all_projects is not None:
        root = (
            ingest.default_projects_root()
            if args.all_projects is ingest.ALL_PROJECTS_DEFAULT_ROOT
            else args.all_projects.expanduser()
        )
        lines, verdict = plan_every_project(root, db_path)
    else:
        projects_dir = (
            args.projects_dir.expanduser()
            if args.projects_dir is not None
            else ingest.default_projects_dir()
        )
        lines, verdict = plan_one_project(projects_dir, db_path)

    for line in lines:
        print(line)
    print(f"verdict: {verdict} -- {VERDICT_NOTES[verdict]}")
    print(NOTHING_HAPPENED)
    # Always 0. Every verdict above is an answer, and a non-zero status would
    # make "this machine holds no transcripts" -- a true finding -- read to a
    # caller as a planner that failed.
    return 0


if __name__ == "__main__":
    sys.exit(main())
