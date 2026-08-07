# Release record

What each release changed **for a user**, and what — if anything — they have to
do about it. Newest first. **Every `##` heading below is one release**, in the
form `## <version> — <date>`; there are no other sections, so an entry cannot
end up hidden behind one. The date is the day the release merged to `main`
(read from `gh pr view` on 2026-08-07, which is also the day both releases
below merged).

This is not a changelog of commits, and it does not restate diffs.
[`versioning.md`](versioning.md) decides *whether* a change is breaking; this
file is where a change so classified is **announced**. One is the rule, the
other is the event, and until this file existed only the rule had a home — the
version number could correctly say "this release broke you" with nowhere for
anyone to read what broke.

**Corrections are not kept here**, deliberately. A figure that was wrong and is
now right goes on [the record in
`README.md`](../README.md#the-record-the-defect-that-made-the-headline-numbers-wrong),
in full, with what the number was and how the difference was measured — a
tool about measurement should show its own corrections beside the number rather
than as a one-line entry in a list. A **break** is a different thing from a
wrong number: it is behaviour a caller's script depended on that no longer
holds, and someone whose script now exits non-zero is not reading a record of
corrected figures. So the two records stay separate, and each links the other
rather than dead-ending.

`cpb.VERSION` is the authority for which release you are running
(`python3 cpb.py --version`), and `/api/summary` reports the same string as
`build.version`. **`tests/test_release_notes.py` asserts that the version this
build ships has an entry below**, so the record cannot quietly fall a release
behind. That is the same mechanism that pins the version spans in `README.md`,
`CLAUDE.md` and `versioning.md`, applied for the same reason: a manual release
step on this project has already rotted twice, and a record CI cannot see will
drift.

**The record begins at 2.0.0**, the first release that had a break to announce.
0.1.0 through 1.6.0 all landed on 2026-08-05 (from `git log` over `cpb.py`,
read 2026-08-07) and are not reconstructed here — 1.x is where the SemVer rule
was adopted and first applied, and the corrections made along the way are
already on the README record. The test does not require entries for historical
versions: pinning history would force a rewrite at every release, which is how
a check becomes something people route around.

## 2.1.0 — 2026-08-07

**No break. Nothing to migrate from 2.0.0.** Every command, flag, exit status,
route and payload field behaves exactly as it did. A release with nothing to
migrate is a useful fact and is worth stating, because otherwise the only way
to learn that a version is safe to take is to read its diff.

**If 2.1.0 is your first install, read the 2.0.0 entry below** — that release's
change to `--prune-missing` is the behaviour you are getting, and this is the
first version installable without cloning, so for most users it arrives here
rather than there.

**CPB became installable from its own marketplace.** The repository is now both
the catalog and the plugin, so there is no clone step:

```
/plugin marketplace add vlad-ko/claude-piggy-bank
/plugin install claude-piggy-bank@claude-piggy-bank
```

The name is doubled because `<plugin>@<marketplace>` names two things that are
here the same repository. The plain checkout is unchanged and still supported;
see [Install](../README.md#install).

**`/cpb` moved file, not name.** The skill was `commands/cpb.md` and is now
`skills/cpb/SKILL.md`
([#73](https://github.com/vlad-ko/claude-piggy-bank/issues/73)), the layout the
plugin reference asks new plugins to use. What you type is
`/claude-piggy-bank:cpb` before the move and after it. It is still a release,
because the shipped artifact changed and an install that never received it
would be running a layout the docs no longer describe.

Merged as [PR #100](https://github.com/vlad-ko/claude-piggy-bank/pull/100).
Minor: a new capability with all three governed surfaces intact.

## 2.0.0 — 2026-08-07

**Breaking: `ingest.py --prune-missing` no longer deletes without being asked,
and refuses outright where nobody can be asked.**

What changed, in the terms you meet it in:

| where you run it | before 2.0.0 | 2.0.0 onwards |
|---|---|---|
| at a terminal | deleted immediately, no prompt and no count | prints a census — the sources, the rows per table, and the date range of the calls involved — then requires you to type `delete` |
| in a pipe, a cron job, a hook or any run whose stdin is not a terminal | deleted immediately, **exit 0** | deletes nothing, **exit 1** with `REFUSED: --prune-missing needs confirmation and stdin is not a terminal` |

**The migration is `--yes`.** It answers the confirmation in advance and exists
for exactly this case: `python3 ingest.py --prune-missing --yes` deletes what
the old command deleted and exits 0. The census still prints, so an unattended
run leaves a record of what went. `--dry-run` is new alongside it and prints
the identical census while deleting nothing.

If you have a script that ran `--prune-missing` unattended, it is currently
exiting 1 and pruning nothing; adding `--yes` restores the previous behaviour.
Nothing was lost while it was failing — a refused prune archives instead, so
those sources are excluded from "currently on disk" coverage and kept in every
historical total.

**Why the change exists.** `--prune-missing` is the one operation in CPB that
destroys measurements nothing can regenerate. Claude Code deletes transcripts
after `cleanupPeriodDays` (default 30), so past that window the rows in the
database are the only surviving copy of that history and no re-ingest brings
them back. Making the deletion opt-in protected against the default; it did not
protect against a typo or a half-remembered flag, on a flag whose blast radius
is a month of history. And a stream that cannot be asked has not said yes: a
non-interactive run refuses rather than reporting `0` over a deletion that did
not happen, because reporting a run that measured nothing as a run that
measured zero is the exact failure this project exists not to commit.

**Why major rather than patch.** [`versioning.md`](versioning.md) puts exit
statuses inside the governed command-line surface — breaking "by definition
rather than by convention". Calling this a patch on the grounds that it only
makes a destructive command safer would need an exemption the rule does not
have and should not grow: "correction is not redefinition" is about figures
that were wrong, not about behaviour a script depends on.

**Also in this release, not breaking.** `/api/summary` gained a `build` block
carrying `version`, and the report names the build that produced its numbers,
so a screenshot or a bug report can say which one it came from. A new field,
with nothing removed, renamed or redefined — minor on its own; the larger bump
governs.

Merged as [PR #98](https://github.com/vlad-ko/claude-piggy-bank/pull/98),
closing [#92](https://github.com/vlad-ko/claude-piggy-bank/issues/92).
