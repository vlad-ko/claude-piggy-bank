# What each figure means

Every number on the page, what it deliberately refuses to say, and why. The
one-screen version — what a first run looks like against a settled one — is in
[`README.md`](../README.md#what-you-get).

- [How big your calls are, and whether that is a lot](#how-big-your-calls-are-and-whether-that-is-a-lot)
- [Two provenances, kept apart on purpose](#two-provenances-kept-apart-on-purpose)
- [What the bands refuse to say](#what-the-bands-refuse-to-say)
- [How old the data is](#how-old-the-data-is)
- [Which projects a total covers](#which-projects-a-total-covers)
- [Absence is never rendered as a value](#absence-is-never-rendered-as-a-value)
- [There are no dollar figures](#there-are-no-dollar-figures)

## How big your calls are, and whether that is a lot

The headline card is **`Median context/call`**. It used to be the mean, and the
change is not cosmetic: measured 2026-08-05 over the reference corpus, the mean
was 237,153 tokens against a median of 155,255 — **1.53x** — with only **28.7%**
of calls above the mean. A figure that 71.3% of calls fall below is not
describing them. The mean is still on the page, but only as **evidence of the
skew** it demonstrates, printed beside the ratio and the share above it.

A context size on its own is a fact with no referent — nothing says whether
266.6k is a lot. Self-comparison cannot supply one, because a percentile of your
own calls compares waste to waste and says nothing if every call is wasteful. So
the referent is external: each call's context is divided by **that model's own
documented context window**, a published hard limit, and the calls are grouped
into four bands.

| band | reading |
|---|---|
| 90% or more of the window | probably wrong |
| 50–90% | likely wasteful |
| 25–50% | *(no verdict)* |
| under 25% | *(no verdict)* |

**Only the top two bands carry a verdict, because only those two were judged.**
A call under half its window was not judged at all, so its label is a range and
nothing more; a word there would invent a verdict nobody decided.

## Two provenances, kept apart on purpose

That paragraph mixes two kinds of claim, and the page never lets them blur:

- **The window is documented.** Per-model, from Anthropic's published model
  overview, carrying the date it was last checked (`WINDOWS_AS_OF`, currently
  2026-08-05). It is per-model rather than one constant because the difference
  is large: Haiku's window is 200K where the current Opus, Sonnet and Fable
  families are 1M. Measured 2026-08-05, Haiku's largest call on the reference
  corpus carries 111,700 tokens — 55.9% of its real window, but 11.2% of a
  wrongly assumed 1M one, a 5x misread that moves the call across two band
  boundaries.
- **The band boundaries are not Anthropic's.** Anthropic publishes the window;
  it publishes no guidance that half a window is wasteful. Where the boundaries
  sit is a **product-owner judgment**, separately dated (`BANDS_AS_OF`), and the
  page says so in those words.

The two travel across the API as two fields with two dates, and are rendered as
two separate statements with the judged one visually marked. Presenting a
judgment in a documented fact's voice would borrow an authority this project has
not earned — which is the whole reason the split exists.

**And the split is per boundary, not per table.** Wherever CPB shows several
judgments together, each one carries its own provenance. One "sources" line at
the foot of a table is not enough, because you will read it as covering the row
you happen to be looking at — so a judged number sitting beside a cited one
would quietly inherit its credibility. The case that settled it is the
recommendation table in
[#78](https://github.com/vlad-ko/claude-piggy-bank/issues/78), which shipped it.
It keys advice on ranges, and two of its boundaries are different kinds of
thing: **1.0** cache reads per write is *documented*, because a single cache
read already repays the 5-minute cache-write markup (the arithmetic and its
source are in [TA-8](claude-api-token-accounting.md#ta-8), checked 2026-08-04),
while **0.25** main-session saturation has **no source at all** and is a dated
judgment. Two numbers that look alike and are not alike, so each says for itself
which it is.

## What the bands refuse to say

- **A model CPB has no window for keeps its context and loses its utilisation.**
  Its size was measured, so it counts in the spread; its window is not something
  this tool knows, so it is counted and **named** as `UNKNOWN, not low` rather
  than banded against a guess. Defaulting to 1M would file every Haiku call four
  bands too low.
- **A call with no context accounting at all is `UNMEASURED, not zero`.** It
  stays out of the median, the mean and the bands. Banded, it would file as the
  most frugal call in the corpus.
- **Calls measuring above 100% of their window read `INCONCLUSIVE`,** and say
  that this build's window table has gone stale — treat the bands as suspect
  rather than the calls as extraordinary. That is the point of a hand-maintained
  *denominator*: a stale window fails loudly and absurdly, where a stale rate
  would have failed silently inside a plausible number.

Banded + unknown + unmeasured is the window's whole call count, and the
denominator of every band share is published beside them.

## How old the data is

`serve` reads whatever the database holds, and `ingest` never runs on its own —
so the page is exactly as current as your last ingest, and one left open for a
week would otherwise look identical to one opened a second ago.

Directly above the totals it states **two facts, never merged into one
"data age"**:

| line | what it means |
|---|---|
| **Last ingest** | when `ingest` last completed — when this tool last *looked* at the transcripts |
| **Newest measured call** | the most recent API call in the database — the newest thing it *found*, across **all** ingested data, not the selected window |

Both, because either alone misleads. A fresh ingest on a machine you have not
used since lunch is perfectly healthy and shows an old newest-call. A database
nobody has re-ingested for a week can show a recent newest-call — for the last
thing it ever saw. Only the pair is readable.

**Past 15 minutes since the last ingest, a banner goes up** in the same place as
the parse-quality and archived-source warnings, saying the figures describe the
transcripts as of then rather than as of now. The threshold is measured, not
taste: re-ingesting is incremental, and on the largest corpus available here
(2,891 transcripts, 1.9 GB, macOS, checked 2026-08-04) an all-skipped re-run
took **1.8 s** against **39.9 s** for a cold full parse — so anyone re-running
ingest on any reasonable cadence never sees it. It marks neglect, not latency.
The warning applies to the *ingest run* only; an idle machine that produced no
calls for hours is not stale.

A database written before CPB recorded ingest times (schema v6 and earlier)
reads **"Last ingest: not recorded"**. That is an *unknown* age, not an age of
zero and not a permanent staleness warning — run ingest once and it starts
recording. Upgrading an older database does not re-parse anything: every hop
from v6 to the current shape is applied in place, and **no measurement is lost
on any of them** — the run-stamp table is added, the retired cost column is
dropped, the format-census table is added, duplicate dispatch rows are resolved
to one row per dispatch (a dispatch recorded by two transcripts is one
dispatch, and the discarded rows are counted out loud), and the cache-miss
diagnostic columns are added empty, because no row written before CPB read them
ever measured one. (Dropping a column needs SQLite 3.35+, which CPB detects at
runtime; on an older library it falls back to a full rebuild, which still
refuses outright if any tracked source has already been reaped.)

There is no automatic refresh yet — refreshing means running ingest again.
Scheduling that from inside `serve.py` is [issue
#20](https://github.com/vlad-ko/claude-piggy-bank/issues/20)'s second half and
ships separately.

## Which projects a total covers

Claude Code keys transcripts on the **working directory**, so every repo — and
every git worktree — is its own project. One database can therefore hold
several of them: `CPB_DB` points every invocation at one file, and the plugin
resolves one database per *install*, so a plugin enabled at user scope ingests
every project you open into the same place.

When that happens every headline figure is a sum across those projects, and the
**scope line** above the totals says so rather than letting a cross-project
total read like one repo's. It states two counts, because either alone
misleads:

| count | what it means |
|---|---|
| projects **in this period** | how many projects the figures on screen actually range over |
| projects **in this database** | how many the file holds at all — a project you ingested but did not use in the selected window is a measured **zero**, not an absent project |

A database with **one** project says nothing new: the line is unchanged, and no
dimension is announced that your data does not have. Calls whose transcript
path does not match the layout in [`data-sources.md`](data-sources.md) are
counted and named separately — they stay in the totals, and they are never
folded into a neighbouring project.

Project *names* are not printed on the page. A project directory is your
absolute working directory with the separators folded to `-`, so it carries
your username and your repo names; `/api/summary` lists them under
`scope.projects` when you want them.

This is the honesty floor, not the feature: filtering and grouping by project
is [issue #7](https://github.com/vlad-ko/claude-piggy-bank/issues/7).

## Absence is never rendered as a value

A read that cannot produce a trustworthy answer refuses — null, inconclusive, or
a loud operator-visible message — rather than returning a plausible number. A
parse failure is counted and surfaced, never silently coerced to `0`. A session
with no subagent transcripts on disk reports "not measured", a different fact
from a measured zero, and the two stay distinguishable everywhere.

The sharpest case: Claude Code persists thinking blocks with **empty text**, so
their size is recorded as *unknown* rather than `0`. Recording `0` would make a
composition table state that thinking costs nothing — false, and invisible,
because it looks like a measurement.

This is the rule everything here is built on, and the bands above are it applied
to the newest surface: unknown model, unmeasured call and over-100% window each
get their own honest answer instead of a number.

The same rule governs how small a sample may be before CPB will judge it. Every
banded metric carries a floor; below it you get the reading and no verdict,
marked `TOO FEW` or **"Not enough data yet"** rather than green. A share over
*n* calls moves in steps of `1/n`, so its floor falls out of the narrowest band
it is judged against; a ratio of two token sums has no such step, so its floor
is judged, dated, and says so. The reasoning is in the 3.1.0 entry of
[`releases.md`](releases.md).

## There are no dollar figures

CPB reports measured tokens and never converts them into money. It used to:
list-rate arithmetic over a hand-maintained rate table, which modelled no
subscription accounting, discount or overage, went stale twice, and on one real
session produced ~$57 where the subscription-accounted spend was ~$21 — over
2.5x out. A precise-looking number that is wrong by a factor of two is worse
than no number, because the reader has no way to see the error; that is the rule
above turned on the tool's own headline, so the estimate was removed rather than
qualified ([#30](https://github.com/vlad-ko/claude-piggy-bank/issues/30)).

What replaced it is the honest version of the same question: panels that
claimed to rank "by spend" rank by **total tokens** and say so in the heading,
with the **model** shown beside each row. Tokens are not tiers — a large Haiku
dispatch can outrank a small Opus one — and the reader weighs that themselves
rather than trusting a derived figure the tool cannot keep current.
