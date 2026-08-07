"""A dated recommendation table keyed on ranges (#78).

The report can already say `main-thread calls over half the window: 38.9%`. It
cannot say what to do about it, and the two ways of closing that gap are both
refused here: a model in the loop is out (constraint 3, and the same corpus
would not yield the same advice twice), and prose hard-coded next to the
number is a threshold nobody can find, date or redline.

So: one table. Each measured metric owns an ordered set of RANGES; a value
falls in exactly one; the entry there names a severity and what to do. The
lookup is arithmetic, the advice is data. The same corpus always yields the
same advice, a changed corpus yields different advice, and a fixed problem
makes its recommendation STOP firing -- none of which needs a model.

This module is a leaf, mirroring `context_window.py`: a dated table, a stated
provenance, and a lookup that returns `None` rather than a default when it
cannot answer. Only `serve.py` should read it, and `index.html` performs no
lookup and holds no threshold.

**Every boundary in this table is a DRAFT pending owner review.** The numbers
below were measured on this project's own corpus on 2026-08-05 and the cut
points around them are a first pass, deliberately in one editable place so
that redlining them is a diff to this file and nothing else. What is being
agreed by shipping this is the STRUCTURE; the numbers are expected to move.

**Provenance is per BOUNDARY, not per table.** This is the reason the module
exists rather than a dict literal. Of the sixteen boundaries here, exactly three
are documented facts with a citation -- `1.0` and `2.0` reads per write, where
TA-8's arithmetic puts the two cache-write break-evens, and `1.0` on the
TTL-aware ratio below, where those same two break-evens land once each write is
weighted by the TTL it asked for. Five are domain floors, which arithmetic fixes
and nobody decided. The remaining eight are product-owner judgments with no
source anywhere. Anthropic publishes the cache multipliers; it publishes nothing
about what share of a window is wasteful and nothing at all about what a user
should do next.

If those sat under one table-level provenance, the judged `0.25` would inherit
the credibility of the cited `1.0` and the page would have no way to tell them
apart. That is exactly the borrowed authority `band_provenance` was introduced
to refuse (#31), reappearing one level down -- so each boundary carries its own
`Provenance`, a citation carries its source and check date and says which fact
it covers, and a judgment structurally CANNOT carry a source (see
`Provenance.__post_init__`).

**Why cache reads-per-write is TWO metrics, and how the band between the two
cited boundaries was resolved.** The first draft of this table had one boundary,
at `1.0`, citing TA-8 for "a single read repays the write markup". TA-8 does not
say that of every write: it says it of the **5-minute** write (1.25x, break-even
at the first read) and explicitly not of the **1-hour** write (2x, still a loss
at one read, a win at two). It warns in as many words against compressing either
into a slogan.

So the flat ratio carries the two claims that hold **whichever TTL was used** --
below `1.0` no write of either kind is repaid, at or above `2.0` a write of
either kind is -- and between them sat a band declared unresolvable, because
`ingest.py` read only the flat `cache_creation_input_tokens` and no column could
say which TTL a write asked for. That hedge was correct and is now retired:
**#84 ingests `usage.cache_creation`'s per-TTL split**, so which break-even
applies is measured per call.

The band resolves into a SECOND metric rather than into a narrower band, for
three reasons.

  * A read carries no TTL of its own. The split resolves the WRITE side only,
    so "reads per 5-minute write" is not a quantity anything observes; what can
    be computed is how the period's reads compare to what its own mix of writes
    requires. That is a different number from the flat ratio, so it gets a
    different key rather than a redefinition under a stable name
    (`docs/versioning.md`).
  * The flat ratio still has to exist. Every call ingested before #84 reads
    NULL for the split, and a transcript past `cleanupPeriodDays` can never be
    re-ingested to fix that -- so on most databases the flat ratio is the only
    one of the two that can be computed at all, and its band is still the
    honest answer for a number that ranges over both TTLs at once. What changed
    in its entry is the REASON: not "CPB cannot see which TTL", which is no
    longer true, but "one flat total cannot say which, and the reading beside
    it can".
  * The mix is not a constant that could be baked into one boundary. Measured
    2026-08-05, deduped by `message.id` over 170,079 calls, 1-hour writes are
    26.86% of the cache-write tokens the split accounts for -- but they occur
    in only 41 of 3,021 files
    and one session was entirely 1-hour. A corpus-wide average applied as a
    boundary would be exactly wrong for the sessions that sit at either
    extreme, which are the sessions the advice is for.

`REPAID_AT_ITS_OWN_TTL` is therefore a boundary that is CITED where it was
previously a hedge, and its statement says so: neither break-even moved, and
what changed is that CPB can tell which one applies wherever the split was
measured. `cache_write_repayment()` holds the weighting, so the arithmetic the
citation names cannot drift away from the boundary it justifies.

**What the structure guarantees, so a test does not have to catch it later.**

  * The healthy range is an ENTRY, not the absence of one. "Nothing to change
    here" is a positive statement; if healthy were represented by no entry,
    healthy and unmeasured would render identically, which is the rule this
    repository is built on.
  * An UNMEASURED metric yields `None` and is named as unmeasured
    (`Assessments.unmeasured`), never falling through to the healthy range.
    `window_for_model()` returning `None` is the pattern.
  * Ranges are half-open and lower-inclusive (`lower <= v < upper`), the same
    convention as `context_window.BANDS` and `serve.day_bounds()`, so a value
    sitting exactly on a boundary lands in one range deterministically.
  * A non-finite value is REFUSED, not banded. `inf` is what a caller gets from
    dividing by a zero denominator, and a zero denominator is an unmeasured
    metric -- banding it as the worst range would render absence as a value in
    the one place this table is supposed to prevent that.
  * "Reduce your cache reads" is unrepresentable. Cache read is the 0.1x class,
    the discount; advice to shrink it is wrong at every scale. It is not merely
    absent from today's strings: `lever()` refuses to build a reduce-directive
    over a discounted class, and `Recommendation` refuses prose that says it in
    a free-text field. Both raise at import time.
  * Ranking across metrics is DERIVED -- severity, then how far into its range
    the value sits -- so it cannot come from the order entries were typed in.
    #66 depends on that.
  * Every metric SAYS WHAT ITS NUMBER MEANS to a reader (`Metric.means`) and
    WHAT KIND OF NUMBER IT IS (`Metric.unit`), and `Metric.__post_init__`
    refuses one that does neither. Both are properties of the metric, so both
    live beside `measurement`; a metric added later cannot reach a summary row
    as two lines of jargon or as a raw float, because it cannot be built.
    `means` is held to the same prose guard the advice is: it too cannot say
    "reduce your cache reads".

**A READING IS NOT A VERDICT UNTIL THE SAMPLE CAN CARRY ONE (#93).** Every
guarantee above is about a value that was measured. None of them said how MUCH
had to be measured, and the omission had a visible cost: a fresh install with
one session and three replies got five green rows, four green dots and
`cache_reads_per_write` reading "Do not change this." -- an instruction, in the
product owner's voice, from three calls. Every figure was individually correct.
The COMPOSITION asserted a clean bill of health on evidence that could not
support one.

The defect was not that small samples were overlooked. It was that UNMEASURED
was wired to the sample being EMPTY rather than to its being SUFFICIENT, so one
call flipped every dot from unknown to good. The empty corpus was handled
impeccably; the cliff sat between 0 and 1.

So every metric now carries a `Sample`: what its denominator COUNTS, and how
many of those the period must hold before a reading may be banded at all. Below
it the reading is `under-sampled` -- a THIRD state, neither `ok` nor
unmeasured, because a real 0, a metric nobody measured and one measured over
too little are three different things and must not render alike.

**The floor is derived where deriving it is honest, and judged where it is
not.** The two rules are not interchangeable and `Sample.__post_init__` will
not let them blur:

  * `FLOOR_RULE_BAND_GRANULARITY`, for the SHARES. A share over `n` members
    moves in steps of `1/n`, so it can express a value strictly inside a band
    only when `1/n` is finer than that band. The floor is therefore whatever
    makes `1/n` finer than the metric's own NARROWEST BOUNDED BAND -- 11 for
    `main_thread_share_over_half_window` (narrowest band `ok` [0, 0.1)) and 51
    for `cache_write_only_share` (`ok` [0, 0.02)). Nothing is judged: the
    numbers fall out of boundaries the table already carries and move the
    moment one is redlined, which is what keeps them from rotting. Over three
    calls `cache_write_only_share` can only be 0, 1/3, 2/3 or 1, so its `ok`
    band was reachable ONLY at exactly zero -- the green verdict was the
    observation that something did not happen three times, reported in the
    voice of a rate.
  * `FLOOR_RULE_JUDGED`, for the RATIOS OF AGGREGATES. They have no `1/n`
    granularity, so the argument above does not apply and is REFUSED rather
    than stretched: the rule cannot be given to a metric whose unit is not
    `METRIC_UNIT_SHARE`. `RATIO_SAMPLE_FLOOR_PROVENANCE` carries the whole
    argument, including what a derivation would have needed and what the share
    rule returns if applied anyway.

A judged floor SAYS it is judged, through the same `Provenance` the boundaries
use -- so it structurally cannot carry a source, and a reader can tell the
derived floor from the decided one without leaving the page.

**What it does not do.** It describes the present only. A recommendation that
stops firing is indistinguishable from a detector that broke unless the report
compares two periods, which is #77 and is deliberately not here.

No money-shaped figure appears in this module (#30), and the guard in
`tests/test_recommendations.py` is total enough that this paragraph cannot
name the units it is refusing. The multipliers it quotes are token multipliers
-- 1.25x, 2x and 0.1x of the base input-TOKEN count -- which is what TA-8
documents; the currency figure they would imply is exactly the arithmetic this
project deleted rather than qualified.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Iterable, Mapping, Optional

# When the JUDGED boundaries below were last decided. Deliberately a different
# constant from any citation's check date: re-reading Anthropic's cache
# documentation does not re-decide where `0.25` sits, and one date covering
# both would say that it had.
RECOMMENDATIONS_AS_OF = "2026-08-05"

# Where this project records the API facts it has checked, and the entry the
# one cited boundary rests on. `tests/test_recommendations.py` asserts the file
# exists and still carries that heading -- a citation to a section that has
# been renumbered is a citation to nothing.
TOKEN_ACCOUNTING_RECORD = "docs/claude-api-token-accounting.md"
CACHE_ARITHMETIC_ENTRY = "TA-8"

RECOMMENDATION_PROVENANCE = (
    f"product-owner judgment, {RECOMMENDATIONS_AS_OF} -- not an Anthropic "
    "recommendation, except where an individual boundary carries a citation of "
    "its own. Anthropic publishes the cache multipliers and the context window; "
    "it publishes no guidance on what share of one is wasteful, and none at all "
    "on what to do next. Every boundary here is a draft pending owner review."
)

# Comparing one metric's depth-in-range against another's is itself a judgment
# -- it says a value three-quarters of the way into `act` on one metric
# outranks one a quarter of the way into `act` on another. Dated and named
# separately so the page can attribute the ORDER as well as the entries.
RANKING_PROVENANCE = (
    f"product-owner judgment, {RECOMMENDATIONS_AS_OF}: severity first, then "
    "how far into its range the value sits. The table's authoring order is "
    "never consulted, and ties break on the metric key so that a reordered "
    "table cannot silently reorder the advice."
)

# --------------------------------------------------------------------------
# Provenance -- one per boundary
# --------------------------------------------------------------------------

PROVENANCE_CITED = "cited"
PROVENANCE_JUDGED = "judged"
PROVENANCE_STRUCTURAL = "structural"
PROVENANCE_KINDS = (PROVENANCE_CITED, PROVENANCE_JUDGED, PROVENANCE_STRUCTURAL)

_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass(frozen=True)
class Provenance:
    """Where ONE boundary's number came from.

    The three kinds are not interchangeable and the constructor will not let
    them blur:

      * `cited` -- a documented fact. MUST carry a source and MUST say which
        facts or models it covers, and MUST carry the date the source was
        checked. A citation whose coverage is unstated is the one that gets
        applied to the case it was never checked against.
      * `judged` -- a product-owner decision. MUST carry the date it was
        decided and MUST NOT carry a source, because there is none; a judgment
        wearing a URL is the defect this whole module is arranged against.
      * `structural` -- arithmetic that could not have been otherwise (a share
        of a counted set cannot be negative). MUST NOT carry a source and MUST
        NOT carry a check date: there is nothing to re-check, and a date would
        claim a currency it does not have.
    """

    kind: str
    statement: str
    checked: Optional[str] = None
    source: Optional[str] = None
    covers: Optional[str] = None

    def __post_init__(self) -> None:
        if self.kind not in PROVENANCE_KINDS:
            raise ValueError(f"unknown provenance kind: {self.kind!r}")
        if not self.statement.strip():
            raise ValueError("a provenance with no statement explains nothing")
        if self.kind == PROVENANCE_CITED:
            if not self.source:
                raise ValueError("a cited boundary must name its source")
            if not self.covers:
                raise ValueError(
                    "a cited boundary must say which facts or models it covers"
                )
            if not self.checked or not _ISO_DATE.match(self.checked):
                raise ValueError(
                    "a cited boundary must carry the ISO date its source was checked"
                )
        if self.kind == PROVENANCE_JUDGED:
            if self.source is not None:
                raise ValueError(
                    "a judged boundary must not carry a source -- there is none, "
                    "and one would lend it a documented fact's authority"
                )
            if not self.checked or not _ISO_DATE.match(self.checked):
                raise ValueError(
                    "a judged boundary must carry the ISO date it was decided"
                )
        if self.kind == PROVENANCE_STRUCTURAL:
            if self.source is not None or self.covers is not None:
                raise ValueError("a structural boundary cites nothing")
            if self.checked is not None:
                raise ValueError(
                    "a structural boundary has no check date: there is nothing "
                    "to re-check, and a date would imply there were"
                )


def cited(statement: str, *, checked: str, source: str, covers: str) -> Provenance:
    """A boundary that is a documented fact. All four fields are required."""
    return Provenance(
        kind=PROVENANCE_CITED,
        statement=statement,
        checked=checked,
        source=source,
        covers=covers,
    )


def judged(statement: str, *, decided: str = RECOMMENDATIONS_AS_OF) -> Provenance:
    """A boundary that is a dated product-owner decision, with no source."""
    return Provenance(kind=PROVENANCE_JUDGED, statement=statement, checked=decided)


def structural(statement: str) -> Provenance:
    """A boundary that arithmetic fixes -- neither checked nor decided."""
    return Provenance(kind=PROVENANCE_STRUCTURAL, statement=statement)


# The two documented boundaries in the table. TA-8's arithmetic, transcribed
# rather than paraphrased: sending the same prefix n times costs n x 1.0
# uncached against `write + 0.1 x (n - 1)` cached, where the write is 1.25x on
# a 5-minute TTL and 2x on a 1-hour one. At n = 2 the 5-minute case is 1.35
# against 2.00 -- a win, so its break-even is the FIRST read. The 1-hour case
# is 2.10 against 2.00 at n = 2, still a loss, and 2.20 against 3.00 at n = 3,
# a win -- so its break-even is the SECOND read.
#
# Read tokens per write token is that n - 1 when one prefix is written once and
# read back n - 1 times; over a period it is an aggregate of many prefixes and
# holds only on average. Both facts below are stated in the form that survives
# CPB's inability to see which TTL a write asked for.
_CACHE_SOURCE = (
    "https://platform.claude.com/docs/en/build-with-claude/prompt-caching#pricing"
)
_CACHE_COVERS = (
    f"both cache-write TTLs, as recorded in {TOKEN_ACCOUNTING_RECORD} "
    f"{CACHE_ARITHMETIC_ENTRY}: the 5-minute write at 1.25x base input tokens, "
    "the 1-hour write at 2x, the read at 0.1x. The multipliers are uniform "
    "across the published model table. It does NOT cover which TTL any "
    "particular write asked for: that is measured rather than documented -- "
    "CPB reads it per call from `usage.cache_creation` (#84) -- and it is "
    "unmeasured for every call ingested before it, which this flat ratio "
    "ranges over regardless."
)

# The third citation, and the one that resolved the band. Same record, same
# check date, same two break-evens; what is new is that each write token can be
# weighted by the TTL it asked for instead of the two being averaged blind.
_REPAYMENT_COVERS = (
    f"the two break-evens {TOKEN_ACCOUNTING_RECORD} {CACHE_ARITHMETIC_ENTRY} "
    "works out, applied one per TTL: the 5-minute write at 1.25x base input "
    "tokens is repaid by its FIRST read, the 1-hour write at 2x by its SECOND, "
    "the read being 0.1x in both cases. What is documented is those two "
    "break-evens; weighting each measured write token by its own is arithmetic "
    "over token counts CPB reads per call. It does NOT cover which read repaid "
    "which write -- a read carries no TTL of its own, so this holds over a "
    "period on average and never per prefix -- and it does not cover calls "
    "whose split was never measured, which are excluded from both sides rather "
    "than assumed to be of either TTL."
)

UNREPAID_UNDER_EVERY_TTL = cited(
    "under one read per write, no cache write is repaid whichever TTL it "
    "asked for: the cheaper 5-minute write (1.25x) breaks even at exactly one "
    "read -- 1.35 against 2.00 at n = 2 -- and the 1-hour write (2x) needs "
    "two. Below one read, both are a loss.",
    checked="2026-08-04",
    source=_CACHE_SOURCE,
    covers=_CACHE_COVERS,
)

REPAID_UNDER_EVERY_TTL = cited(
    "at two reads per write the markup is repaid whichever TTL it asked for: "
    "the 1-hour write (2x) turns a profit at its second read -- 2.20 against "
    "3.00 at n = 3 -- and the 5-minute write (1.25x) turned one at its first. "
    "Between one read and two, which side of break-even a write sits on "
    "depends on a TTL one flat total cannot name.",
    checked="2026-08-04",
    source=_CACHE_SOURCE,
    covers=_CACHE_COVERS,
)

# How many read tokens one write token has to earn back before it has repaid
# its own markup, one figure per TTL. TA-8's two break-evens in the units this
# module's second cache metric divides in, and DELIBERATELY UNEQUAL: they are
# the whole difference between the two TTLs, and a build that collapsed them
# into one constant would be the slogan TA-8 warns against, in code.
READ_TOKENS_TO_REPAY_A_5M_WRITE_TOKEN = 1.0
READ_TOKENS_TO_REPAY_A_1H_WRITE_TOKEN = 2.0

REPAID_AT_ITS_OWN_TTL = cited(
    "at 1.0 a period's cache reads cover exactly what its writes require at "
    "the break-even of the TTL each one asked for -- one read token per "
    "5-minute write token, two per 1-hour write token. Below it those writes "
    "are not repaid; at or above it they are, and no averaging over the two "
    "TTLs is involved. This boundary was a HEDGE until 2026-08-05: with only "
    "the flat cache-write total ingested, every reading between one read per "
    "write and two turned on a TTL CPB had not read, and the table carried an "
    "unresolvable band there instead of a verdict. Ingesting "
    "`usage.cache_creation` (#84) resolved it. Neither break-even moved; which "
    "one applies stopped being unknown.",
    checked="2026-08-04",
    source=_CACHE_SOURCE,
    covers=_REPAYMENT_COVERS,
)

# Measured 2026-08-05 over 3,021 of this machine's transcripts, counting
# `usage.cache_creation`'s two members on records DEDUPED by `message.id` with
# ingest's own rule -- 342,850 assistant usage records, 170,079 calls:
# 231,497,808 of 861,835,379 split cache-write tokens asked for the 1-hour TTL.
# It supersedes an earlier raw-record scan that put the share at 26% over a
# smaller file set; both are dated samples of a corpus that grows between
# scans, so re-measure before quoting either.
#
# Recorded because it is the evidence for TWO decisions rather than one. That
# the share is a quarter is why a single 1.0 boundary cited to TA-8 would have
# been wrong for a quarter of the corpus. That it is CONCENTRATED -- 1-hour
# writes occur in only 41 of the 3,021 files, and one session was entirely
# 1-hour -- is why the resolution weights each corpus's own measured mix
# instead of baking this number into a boundary: the sessions this advice is
# for are exactly the ones the average describes worst.
ONE_HOUR_WRITE_SHARE_AS_MEASURED = 0.2686

# Why the TTL-aware metric divides by the SPLIT and never by the flat total.
# Measured in the same scan: `cache_write_5m + cache_write_1h` equalled
# `cache_creation_input_tokens` on 170,071 of 170,079 deduped calls. On the
# other 8 -- 49,838 tokens -- the flat total read 0 while the split did not, so
# the flat total UNDERSTATED the write; the reverse occurred 0 times. Ingest
# stores both verbatim and counts the disagreement rather than reconciling it.
#
# The direction is what matters here: an understated denominator makes the flat
# reads-per-write ratio read HIGHER, i.e. healthier, than the writes justify.
# At 49,838 tokens against 861,835,379 that bias is far too small to move any
# boundary in this table today, and it is recorded anyway, because the metric
# that avoids it entirely is worth having for a corpus where it is not.
FLAT_WRITE_UNDERSTATED_CALLS_AS_MEASURED = 8
DEDUPED_CALLS_AS_MEASURED = 170_079

_FLOOR_OF_A_SHARE = structural(
    "domain floor: a share is one count divided by a set containing it, so it "
    "cannot be negative. Arithmetic, not a judgment -- there is nothing here "
    "to redline."
)

_FLOOR_OF_A_RATIO = structural(
    "domain floor: a ratio of two non-negative token counts cannot be "
    "negative. Arithmetic, not a judgment -- there is nothing here to redline."
)


@dataclass(frozen=True)
class Boundary:
    """One cut point, and where its number came from.

    Adjacent ranges share the same `Boundary` object, so the value and its
    provenance are stated once and the two sides cannot drift apart.
    """

    value: float
    provenance: Provenance


# --------------------------------------------------------------------------
# Levers -- what the reader is being told to change
# --------------------------------------------------------------------------

ACTION_REDUCE = "reduce"
ACTION_INCREASE = "increase"
ACTION_VERBS = {ACTION_REDUCE: "Reduce", ACTION_INCREASE: "Increase"}

# Every target advice may name, and the phrase used to render it. A closed
# registry: `lever()` refuses anything not here, so advice cannot invent a
# thing to change that the report never measured.
LEVER_TARGETS: dict[str, str] = {
    "input_tokens": "uncached input tokens",
    "cache_write": "cache-write tokens -- the 1.25x class",
    "cache_read": "cache-read tokens -- the 0.1x class, which is the discount",
    "output_tokens": "output tokens",
    "main_thread_context": "the context the main session carries",
    "subagent_dispatch": "the work handed to subagents",
    "prompt_prefix_stability": "the stability of the prompt prefix between calls",
}

# The token classes that cost LESS than base input. Shrinking one of these
# raises the bill it appears to lower, so no advice may ask for it. Derived
# from TA-8's multipliers rather than written out as a blacklist of strings,
# so a second discounted class would be protected by adding it here and
# nowhere else.
DISCOUNTED_TOKEN_CLASSES = frozenset({"cache_read"})
NON_REDUCIBLE_TARGETS = frozenset(
    target for target in LEVER_TARGETS if target in DISCOUNTED_TOKEN_CLASSES
)

# Free-text prose is checked for the same directive the lever refuses, so it
# cannot be smuggled past `lever()` in an entry's `detail`. Scoped to one
# sentence, so "Reduce the main thread's context. The cache read discount then
# does the rest." stays legal.
_REDUCE_VERBS = (
    "reduce",
    "lower",
    "cut",
    "shrink",
    "minimise",
    "minimize",
    "decrease",
    "fewer",
    "less",
    "avoid",
)
_NON_REDUCIBLE_PHRASES = ("cache read", "cache-read", "cache_read")


@dataclass(frozen=True)
class Lever:
    """The machine-readable half of a recommendation: change WHAT, which way.

    Built only through `lever()`. The rendered directive is composed from the
    action and the registry phrase rather than typed, so the set of directives
    that can exist is exactly the set the registry allows.
    """

    action: str
    target: str

    @property
    def directive(self) -> str:
        return f"{ACTION_VERBS[self.action]} {LEVER_TARGETS[self.target]}"


def lever(action: str, target: str) -> Lever:
    """Build a lever, refusing the ones that are wrong at every scale.

    `lever(ACTION_REDUCE, "cache_read")` raises. Cache read is the discount:
    advice to shrink it would be advice to re-send the prefix uncached at 10x
    the tokens, and the report would be confidently recommending the more
    expensive of two options. Making it unrepresentable is cheaper than
    remembering not to write it.
    """
    if action not in ACTION_VERBS:
        raise ValueError(f"unknown lever action: {action!r}")
    if target not in LEVER_TARGETS:
        raise ValueError(f"unknown lever target: {target!r}")
    if action == ACTION_REDUCE and target in NON_REDUCIBLE_TARGETS:
        raise ValueError(
            f"cannot advise reducing {target!r}: it is a discounted token class "
            "(TA-8), so shrinking it raises the token bill it appears to lower"
        )
    return Lever(action=action, target=target)


# --------------------------------------------------------------------------
# Severities
# --------------------------------------------------------------------------

SEVERITY_OK = "ok"
SEVERITY_WATCH = "watch"
SEVERITY_ACT = "act"

# Higher sorts first. Not alphabetical, not declaration order -- an explicit
# ordering, because ranking reads it.
SEVERITY_RANK = {SEVERITY_OK: 0, SEVERITY_WATCH: 1, SEVERITY_ACT: 2}


@dataclass(frozen=True)
class Recommendation:
    """What one range says: a severity, an optional lever, and the prose.

    `SEVERITY_OK` must have no lever and every other severity must have one.
    A healthy entry that also told the reader to change something would be
    contradicting itself, and a firing entry with nothing to change would be
    an alarm with no action -- the shape that trains readers to ignore a page.
    """

    severity: str
    lever: Optional[Lever]
    detail: str

    def __post_init__(self) -> None:
        if self.severity not in SEVERITY_RANK:
            raise ValueError(f"unknown severity: {self.severity!r}")
        if not self.detail.strip():
            raise ValueError("a recommendation with no detail says nothing")
        if self.severity == SEVERITY_OK and self.lever is not None:
            raise ValueError(
                "a healthy entry must not carry a lever: 'nothing to change' "
                "and 'change this' cannot both be true"
            )
        if self.severity != SEVERITY_OK and self.lever is None:
            raise ValueError(
                f"a {self.severity!r} entry must name the lever to pull; an "
                "alarm with no action is noise"
            )
        _refuse_forbidden_prose(self.detail)

    @property
    def text(self) -> str:
        if self.lever is None:
            return self.detail
        return f"{self.lever.directive}. {self.detail}"


def _refuse_forbidden_prose(detail: str) -> None:
    """Raise if `detail` advises shrinking a discounted token class.

    The lever registry stops the machine-readable directive; this stops the
    same sentence being typed into the prose beside it.
    """
    for sentence in re.split(r"[.;!?]", detail.lower()):
        if not any(phrase in sentence for phrase in _NON_REDUCIBLE_PHRASES):
            continue
        for verb in _REDUCE_VERBS:
            if re.search(rf"\b{verb}\b", sentence):
                raise ValueError(
                    f"prose advises {verb!r} on a discounted token class: "
                    f"{sentence.strip()!r}"
                )


# --------------------------------------------------------------------------
# Ranges and metrics
# --------------------------------------------------------------------------

WORSE_WHEN_HIGHER = "higher"
WORSE_WHEN_LOWER = "lower"
WORSE_WHEN_DIRECTIONS = (WORSE_WHEN_HIGHER, WORSE_WHEN_LOWER)

# WHAT KIND OF NUMBER A METRIC'S READINGS ARE, so a reader is shown `30.3%`
# rather than `0.3034` and `3.20x` rather than `3.195` (#89 review).
#
# A share and a ratio are different kinds of quantity and a reader holds them
# in percent and in multiples, not in four significant figures. The page had
# one formatter for both because nothing told it them apart.
#
# THE PAGE MUST NEVER LEARN A METRIC KEY. An `if (metric === 'cache_reads_per_
# write')` in `index.html` would be a third enumeration of the metric set, in
# the one file no import guard can reach -- so the unit crosses on the payload,
# per reading, and the page holds one formatter per UNIT.
#
# WHY IT LIVES HERE. It shipped in `serve.py` as a parallel `METRIC_UNITS`
# mapping under an import-time totality guard, because the branch that added it
# could not edit this module. A mapping beside a table is one enumeration of a
# set too many, and its own note said where it belonged: the unit is a property
# of the metric, like `measurement` and `worse_when`. The guard's PROPERTY is
# kept and made stronger -- a metric with no unit, or one outside this
# vocabulary, now fails at CONSTRUCTION rather than at `import serve`, so it
# cannot exist even in a caller that never imports the serving layer.
METRIC_UNIT_SHARE = "share"
METRIC_UNIT_RATIO = "ratio"
METRIC_UNIT_COUNT = "count"
# The closed vocabulary. `count` has no metric today and is declared anyway:
# the page carries one formatter per member, and a unit reaching it with no
# formatter is the failure this vocabulary exists to make impossible.
METRIC_UNIT_KINDS = (METRIC_UNIT_SHARE, METRIC_UNIT_RATIO, METRIC_UNIT_COUNT)


@dataclass(frozen=True)
class Range:
    """`lower.value <= v < upper.value`, with `upper=None` meaning no ceiling.

    Lower-inclusive throughout, so a value exactly on a boundary belongs to the
    range ABOVE it and lands in exactly one place. The top range of every
    metric is unbounded: a share of 1.0 is reachable and has to land somewhere,
    and a ratio has no ceiling at all.
    """

    lower: Boundary
    upper: Optional[Boundary]
    recommendation: Recommendation

    def contains(self, value: float) -> bool:
        if value < self.lower.value:
            return False
        return self.upper is None or value < self.upper.value


def depth_in_band(
    value: float, lower: float, upper: Optional[float], worse_when: str
) -> float:
    """How far `value` sits into `[lower, upper)`: 0 at its better end, 1 at
    its worse end.

    A bounded band normalises across its width. An unbounded one has no width
    to normalise against, so it uses the reciprocal of its own entry boundary
    -- `1 - lower/v` where higher is worse, `lower/v` where lower is worse.
    Both agree with the bounded formula at the entry boundary, both are
    monotonic in the direction of harm, and neither invents a ceiling nobody
    decided.

    This is an ORDERING, not a measurement, and `RANKING_PROVENANCE` says so:
    comparing one metric's depth against another's is a judgment about which
    lever to show first, not a claim that the two numbers are commensurable.
    """
    if worse_when not in (WORSE_WHEN_HIGHER, WORSE_WHEN_LOWER):
        raise ValueError(f"unknown direction: {worse_when!r}")
    if value < lower or (upper is not None and value >= upper):
        raise ValueError(f"{value!r} is not in [{lower!r}, {upper!r})")
    if upper is not None:
        fraction = (value - lower) / (upper - lower)
        return fraction if worse_when == WORSE_WHEN_HIGHER else 1.0 - fraction
    if lower <= 0:
        # Would need a ceiling that does not exist. No metric here has an
        # unbounded band starting at zero; this refuses rather than inventing
        # a depth if one is ever added.
        raise ValueError("an unbounded band starting at zero has no derivable depth")
    ratio = lower / value
    return 1.0 - ratio if worse_when == WORSE_WHEN_HIGHER else ratio


# --------------------------------------------------------------------------
# #93: how big a sample a reading needs before it may be banded
# --------------------------------------------------------------------------

# When the JUDGED sample floor below was decided. A THIRD date, deliberately
# not `RECOMMENDATIONS_AS_OF` and deliberately not a citation's check date:
# re-deciding where `0.25` sits does not re-decide how many calls a ratio needs,
# and one date covering both would say that it had.
SAMPLE_FLOOR_AS_OF = "2026-08-07"

# The two rules by which a floor comes to have its number. They are different
# KINDS of claim, exactly as `Provenance`'s three kinds are, and `Sample`
# refuses to let one wear the other's authority.
FLOOR_RULE_BAND_GRANULARITY = "band-granularity"
FLOOR_RULE_JUDGED = "judged"
FLOOR_RULES = (FLOOR_RULE_BAND_GRANULARITY, FLOOR_RULE_JUDGED)

# How many contributing calls a RATIO OF AGGREGATES needs. Judged, and the
# statement below is the argument rather than a gloss on it.
RATIO_SAMPLE_FLOOR = 10

RATIO_SAMPLE_FLOOR_PROVENANCE = judged(
    "ten contributing calls, and this is a JUDGMENT rather than a derivation. "
    "The shares' floor falls out of counting: a call contributes exactly 1 to a "
    "counted numerator, so the reading moves in steps of 1/n and the floor is "
    "whatever makes 1/n finer than the narrowest band it is compared against. A "
    "ratio of two token sums has no such step. One call contributes its own "
    "token count, which is unbounded and, on this project's own measurements, "
    "heavily skewed -- context per call ran 77,128 at p10 against 958,151 at "
    "p99 (2026-08-05, `serve.PERCENTILES`), and the one-hour cache writes that "
    "set the TTL-aware ratio's denominator occurred in 41 of 3,021 files with "
    "one session entirely one-hour. So 'one more call can no longer swing the "
    "reading across a boundary' needs a bound on how much of a period's tokens "
    "one call may carry, which nothing here has established and which those two "
    "figures contradict. DERIVING IT ANYWAY WOULD BE WORSE THAN JUDGING IT: the "
    "share rule applied to these three metrics returns 2, 2 and 1, because "
    "their narrowest bounded bands are a whole unit wide or more -- it would "
    "certify a two-call sample AND wear arithmetic while doing it, which is the "
    "borrowed authority this module is arranged against. Ten is an order of "
    "magnitude of contributing calls, the same voice as the two judged 'the "
    "prefix is doing its job' edges above, and nobody publishes it. What would "
    "redline it is a measurement of how much of a period's tokens one call can "
    "carry.",
    decided=SAMPLE_FLOOR_AS_OF,
)


def smallest_sample_finer_than(width: float) -> int:
    """The smallest `n` for which `1/n` is strictly finer than `width`.

    THE LOOP CONDITION IS THE DEFINITION, which is why the number is right
    however the starting guess lands. `floor(1/width)` is an algebraic
    rearrangement and a float one -- at a width of exactly 0.1 it is the
    difference between 10 and 11, i.e. between certifying a sample that cannot
    express the band and refusing it -- so it is used only to skip iterations,
    one below where the answer can be, and never to produce the answer.

    A share over `n` members moves in steps of `1/n`. To land STRICTLY INSIDE a
    band of `width` rather than only on its edges, it needs a step finer than
    that width; `>=` is the right comparison because a step exactly equal to the
    width reaches the edges and nothing between them.
    """
    if not math.isfinite(width) or width <= 0:
        raise ValueError(f"a band of width {width!r} has no sample floor")
    n = max(1, math.floor(1.0 / width) - 1)
    while 1.0 / n >= width:
        n += 1
    return n


@dataclass(frozen=True)
class SampleFloor:
    """A RESOLVED floor: the number, what it counts, and where it came from.

    `counts` is load-bearing in the same way `Metric.measurement` is. "51" says
    nothing on its own; "51 cache-writing calls" names the set the floor is
    counting, which is the metric's own DENOMINATOR and not the period's call
    count. A floor applied to the wrong denominator is a wrong number that
    reads right -- `main_vs_subagent_tokens_per_reply` needs its floor in BOTH
    scopes, and the pooled count would pass it on a corpus that dispatched one
    subagent.
    """

    minimum: int
    counts: str
    rule: str
    provenance: Provenance


@dataclass(frozen=True)
class Sample:
    """WHAT a metric's denominator counts, and by WHICH RULE its floor is set.

    The spec, not the resolved floor: a band-granularity floor has no number of
    its own to state, because its number is arithmetic over the metric's own
    ranges and must move when they do. `Metric.sample_floor` resolves it.

    The constructor refuses every blurring of the two rules:

      * a derived floor MUST NOT carry a number or a provenance -- both are
        computed, and a typed-in number would be free to drift from the bands
        it claims to come from;
      * a judged floor MUST carry both, and its provenance must be `judged`,
        which structurally cannot carry a source.

    Whether the rule is ALLOWED at all is checked one level up, in
    `Metric.__post_init__`, because it turns on the metric's unit.
    """

    counts: str
    rule: str
    minimum: Optional[int] = None
    provenance: Optional[Provenance] = None

    def __post_init__(self) -> None:
        if not self.counts.strip():
            raise ValueError(
                "a sample floor that does not name what it counts is a number "
                "against an unnamed denominator"
            )
        if self.rule not in FLOOR_RULES:
            raise ValueError(f"unknown sample floor rule: {self.rule!r}")
        if self.rule == FLOOR_RULE_JUDGED:
            if not isinstance(self.minimum, int) or self.minimum < 1:
                raise ValueError(
                    "a judged sample floor must state its own minimum, and a "
                    "floor below one member would admit a reading over nothing"
                )
            if self.provenance is None or self.provenance.kind != PROVENANCE_JUDGED:
                raise ValueError(
                    "a judged sample floor must carry a judged provenance: it "
                    "is somebody's decision and has to say so"
                )
        if self.rule == FLOOR_RULE_BAND_GRANULARITY:
            if self.minimum is not None or self.provenance is not None:
                raise ValueError(
                    "a derived sample floor states neither number nor "
                    "provenance: both are computed from the metric's own "
                    "bands, and a typed one could drift from them"
                )


def band_granularity_floor(ranges: tuple["Range", ...], counts: str) -> SampleFloor:
    """The floor a SHARE needs to express a value strictly inside its bands.

    Against the NARROWEST BOUNDED band, because a floor that satisfied only the
    widest would leave the narrow one reachable at its edges alone -- which is
    exactly `cache_write_only_share`'s `ok` band being reachable only at
    literal zero over three calls.

    The unbounded top range has no width to be finer than and is skipped: a
    reading there needs no interior resolution, and inventing a ceiling to give
    it one is the refusal `depth_in_band()` already makes.

    The provenance is STRUCTURAL. There is nothing here to re-check and a date
    would claim a currency it does not have: the number moves by itself when a
    boundary is redlined. Its INPUT may well be a judgment -- both of these
    narrowest bands are judged edges -- and the statement says so, so that a
    reader is not told the floor is beyond argument when the band it came from
    is not.
    """
    widths = [
        (entry.upper.value - entry.lower.value, entry.lower.value, entry.upper.value)
        for entry in ranges
        if entry.upper is not None
    ]
    if not widths:
        raise ValueError(
            "every range is unbounded, so there is no band width to be finer "
            "than and no granularity floor to derive"
        )
    width, lower, upper = min(widths)
    minimum = smallest_sample_finer_than(width)
    return SampleFloor(
        minimum=minimum,
        counts=counts,
        rule=FLOOR_RULE_BAND_GRANULARITY,
        provenance=structural(
            f"derived, not decided: this reading is a share over {counts}, so it "
            f"moves in steps of 1/n and can express a value strictly inside a "
            f"band only where 1/n is finer than that band. Its narrowest bounded "
            f"band is [{lower:g}, {upper:g}), width {width:g}, so n must exceed "
            f"{1.0 / width:g} -- {minimum}. Arithmetic over boundaries this table "
            "already carries, and it moves the moment one of them is redlined, "
            "which is what keeps it from rotting. The BAND it is computed from "
            "may itself be a judgment; this floor is not a second one."
        ),
    )


@dataclass(frozen=True)
class Metric:
    """One measured quantity and the ranges over it.

    `measurement` is the load-bearing field: it names exactly what was divided
    by what, so a reader can check the advice against the number and the two
    cannot drift apart. `serve.py` computing something else under this key is
    then a visible contradiction rather than a silent one.

    `means` IS NOT A SECOND `measurement` AND DOES NOT REPLACE IT. The two
    answer different questions and both are needed: `measurement` says what was
    divided by what, which is what an auditor checks the figure against;
    `means` says what the figure tells the person reading it, which is what a
    summary row needs. The summary showed `measurement` because nothing else
    existed -- "share of main-thread API calls whose context reaches at least
    half the model's documented window (context_window bands 50-to-90 and
    at-least-90), over main-thread calls with a known window" is exactly right
    one level down and is two lines of jargon on a row of advice.

    Written HERE rather than in `index.html` for the reason no advice on that
    page is authored there: a sentence about what a metric means is a claim
    about the measurement, and one typed into the page would have no date, no
    owner and nothing to check it against. It is held to the same prose guard
    the advice is (`_refuse_forbidden_prose`), so "reduce your cache reads"
    cannot be smuggled in through the plain-English field either.

    `unit` says what KIND of number the readings are, so the page can print a
    share as a percentage and a ratio as a multiple without ever learning a
    metric key. See `METRIC_UNIT_KINDS`.

    `sample` says HOW MUCH has to be measured before a reading may be banded at
    all (#93), and it is not optional: a metric with no floor is one whose first
    call earns it a verdict, which is the defect the field exists to close.

    `__post_init__` refuses a metric missing any of them. A metric added later
    cannot ship with no reader copy, with an unrecognised unit, with a direction
    nothing can read or with no sample floor: it would not be constructible,
    which is stronger than a guard that fires when some other module is
    imported. It also resolves the floor EAGERLY, so a metric whose bands admit
    no derivable floor fails where it is written rather than at the first
    request that reads one.
    """

    key: str
    measurement: str
    means: str
    unit: str
    worse_when: str
    ranges: tuple[Range, ...]
    sample: Sample

    def __post_init__(self) -> None:
        if not self.key.strip():
            raise ValueError("a metric with no key cannot be looked up")
        if not self.measurement.strip():
            raise ValueError(
                f"{self.key}: a metric with no measurement names nothing that "
                "was divided by anything, so no reader can check it"
            )
        if not self.means.strip():
            raise ValueError(
                f"{self.key}: a metric with no reader sentence reaches a "
                "summary row as its `measurement`, which is a specification "
                "and not a sentence. Say what the number means to the person "
                "reading it, beside `measurement` and never instead of it"
            )
        _refuse_forbidden_prose(self.means)
        if self.unit not in METRIC_UNIT_KINDS:
            raise ValueError(
                f"{self.key}: unit {self.unit!r} is not one of "
                f"{list(METRIC_UNIT_KINDS)}. The page carries one formatter per "
                "unit, so a reading in an unnamed one prints as a raw float"
            )
        if self.worse_when not in WORSE_WHEN_DIRECTIONS:
            raise ValueError(f"{self.key}: unknown direction {self.worse_when!r}")
        if not self.ranges:
            raise ValueError(f"{self.key}: a metric with no range assesses nothing")
        # THE SHARES' DERIVATION MAY NOT BE GIVEN TO A RATIO, and refusing it
        # here is what makes "do not hand the ratios the number that is to
        # hand" unrepresentable rather than merely discouraged -- `lever()`'s
        # treatment of "reduce your cache reads", one field over. The rule
        # rests on a call contributing exactly 1 to a counted numerator, which
        # is what `METRIC_UNIT_SHARE` means here and what a ratio of token sums
        # is not. It is not caught by arithmetic: applied to the three ratios
        # the rule RETURNS something -- 2, 2 and 1 -- and a number that looks
        # derived is worse than no number at all.
        if (
            self.sample.rule == FLOOR_RULE_BAND_GRANULARITY
            and self.unit != METRIC_UNIT_SHARE
        ):
            raise ValueError(
                f"{self.key}: the band-granularity floor is only honest for a "
                f"{METRIC_UNIT_SHARE!r}, whose value moves in steps of 1/n. "
                f"This metric is a {self.unit!r} -- a ratio of aggregates has "
                "no such step, so use FLOOR_RULE_JUDGED and say whose judgment "
                "it is"
            )
        # Resolve now, so an underivable floor fails at construction.
        self.sample_floor

    @property
    def sample_floor(self) -> SampleFloor:
        """How many members this metric's sample needs, resolved.

        Derived from the metric's own ranges under `FLOOR_RULE_BAND_GRANULARITY`
        and read off the spec under `FLOOR_RULE_JUDGED`. `Sample` has already
        refused every combination that is not one of those two, so there is no
        third branch and no default.
        """
        if self.sample.rule == FLOOR_RULE_BAND_GRANULARITY:
            return band_granularity_floor(self.ranges, self.sample.counts)
        assert self.sample.minimum is not None  # `Sample` refuses otherwise
        assert self.sample.provenance is not None
        return SampleFloor(
            minimum=self.sample.minimum,
            counts=self.sample.counts,
            rule=self.sample.rule,
            provenance=self.sample.provenance,
        )

    def range_for(self, value: float) -> Range:
        for entry in self.ranges:
            if entry.contains(value):
                return entry
        raise ValueError(f"no range covers {value!r} for metric {self.key!r}")

    def severity_band(self, entry: Range) -> tuple[float, Optional[float]]:
        """The span of the contiguous run of ranges sharing `entry`'s severity.

        Depth is measured across this, not across the single range, and the
        difference is not cosmetic. `cache_reads_per_write` splits `watch` at
        the second cited break-even into a narrow range (1-2) and a wide one
        (2-10); measured per range, a reading of 2.5 would come out DEEPER into
        `watch` than a reading of 1.5, which is worse. Ranking would then show
        the better corpus as the bigger lever. Measured across the band, depth
        is monotonic in harm over the whole severity, which is what the word
        means.
        """
        # By identity, not equality: `range_for()` hands back the object it
        # matched, and two ranges that happened to compare equal would put the
        # band on whichever came first.
        index = next(i for i, r in enumerate(self.ranges) if r is entry)
        severity = entry.recommendation.severity
        first = index
        while first > 0 and self.ranges[first - 1].recommendation.severity == severity:
            first -= 1
        last = index
        while (
            last + 1 < len(self.ranges)
            and self.ranges[last + 1].recommendation.severity == severity
        ):
            last += 1
        upper = self.ranges[last].upper
        return self.ranges[first].lower.value, None if upper is None else upper.value

    def depth(self, value: float) -> float:
        """How far into its SEVERITY BAND `value` sits, 0 better, 1 worse."""
        lower, upper = self.severity_band(self.range_for(value))
        return depth_in_band(value, lower, upper, self.worse_when)


METRIC_MAIN_THREAD_SHARE_OVER_HALF_WINDOW = "main_thread_share_over_half_window"
METRIC_CACHE_READS_PER_WRITE = "cache_reads_per_write"
METRIC_CACHE_WRITE_REPAYMENT_AT_OWN_TTL = "cache_write_repayment_at_own_ttl"
METRIC_CACHE_WRITE_ONLY_SHARE = "cache_write_only_share"
METRIC_MAIN_VS_SUBAGENT_TOKENS_PER_REPLY = "main_vs_subagent_tokens_per_reply"

# Boundaries, named so the two ranges that meet at one share the object.
# Every value below is a DRAFT. The measured figure that motivated each is in
# the comment beside it -- this project's own corpus, 2026-08-05.
_MAIN_SHARE_WATCH_EDGE = Boundary(
    0.10,
    judged(
        "below one call in ten over half its window, the main session is not "
        "the thing to look at first."
    ),
)
_MAIN_SHARE_ACT_EDGE = Boundary(
    0.25,
    judged(
        "a quarter of main-thread calls over half the window is where handing "
        "work to subagents starts to pay for the handover. Nobody publishes "
        "this number; it is a first draft over a corpus measuring 0.389."
    ),
)
_READS_PER_WRITE_UNREPAID_EDGE = Boundary(1.0, UNREPAID_UNDER_EVERY_TTL)
_READS_PER_WRITE_REPAID_EDGE = Boundary(2.0, REPAID_UNDER_EVERY_TTL)
_READS_PER_WRITE_HEALTHY_EDGE = Boundary(
    10.0,
    judged(
        "ten reads per write is 'the prefix is doing its job', chosen as an "
        "order of magnitude above the later of the two documented break-evens "
        "rather than derived from anything. A corpus measuring 55.0 sits well "
        "clear of it, which is the only evidence behind the number."
    ),
)
_REPAYMENT_BREAK_EVEN_EDGE = Boundary(1.0, REPAID_AT_ITS_OWN_TTL)
_REPAYMENT_HEALTHY_EDGE = Boundary(
    10.0,
    judged(
        "ten times what a period's writes require is 'the prefix is doing its "
        "job', an order of magnitude above the break-even and mirroring the "
        "flat ratio's own judged edge. Deliberately NOT the same demand in "
        "reads: a corpus whose writes all asked for the one-hour TTL needs "
        "twenty reads per write to reach it against ten on an all-five-minute "
        "one, because the one-hour write starts twice as deep. Nobody "
        "publishes this number, and unlike the flat ratio's ten it has no "
        "corpus behind it yet -- the readings this table was drafted against "
        "predate the split and are unmeasured here."
    ),
)
_WRITE_ONLY_WATCH_EDGE = Boundary(
    0.02,
    judged(
        "one stored prefix in fifty never read is background: sessions that "
        "end on their first call produce these and no change would prevent it."
    ),
)
_WRITE_ONLY_ACT_EDGE = Boundary(
    0.10,
    judged(
        "one in ten stored prefixes never read is a pattern rather than an "
        "accident. TA-8 makes each of those a 1.25x write for nothing, but "
        "what SHARE of them is worth acting on is published by nobody."
    ),
)
_REPLY_RATIO_WATCH_EDGE = Boundary(
    1.5,
    judged(
        "half again as much per main-thread reply is expected of a session "
        "that coordinates, and is not worth a word."
    ),
)
_REPLY_RATIO_ACT_EDGE = Boundary(
    3.0,
    judged(
        "three times as much per main-thread reply says the main thread is "
        "doing work a subagent could do at a third the tokens. A corpus "
        "measuring 4.0 is what prompted the number, not what justifies it."
    ),
)

METRICS: dict[str, Metric] = {
    METRIC_MAIN_THREAD_SHARE_OVER_HALF_WINDOW: Metric(
        key=METRIC_MAIN_THREAD_SHARE_OVER_HALF_WINDOW,
        measurement=(
            "share of main-thread API calls whose context reaches at least half "
            "the model's documented window (context_window bands 50-to-90 and "
            "at-least-90), over main-thread calls with a known window"
        ),
        # "at least half", not "more than half": the bands this counts are
        # 50-to-90 and at-least-90, so a call sitting exactly on half is one of
        # these.
        means=(
            "How often your main session is already carrying at least half its "
            "context window when it calls the API. Past that point every reply "
            "re-reads a long history."
        ),
        unit=METRIC_UNIT_SHARE,
        # THE DENOMINATOR, NOT THE PERIOD'S CALLS. `banded_calls` for the
        # main thread -- a call is banded exactly when its context was measured
        # AND its model has a documented window -- which is the set this share
        # divides by. The period may hold a thousand calls and eleven of them
        # be banded, and it is the eleven that decide whether this reading can
        # sit anywhere but on a band edge.
        sample=Sample(
            counts="main-thread calls with a measured context and a documented window",
            rule=FLOOR_RULE_BAND_GRANULARITY,
        ),
        worse_when=WORSE_WHEN_HIGHER,
        ranges=(
            Range(
                lower=Boundary(0.0, _FLOOR_OF_A_SHARE),
                upper=_MAIN_SHARE_WATCH_EDGE,
                recommendation=Recommendation(
                    severity=SEVERITY_OK,
                    lever=None,
                    detail=(
                        "The main session stays lean: almost every call sits in "
                        "the lower half of its window, so nothing is being "
                        "re-sent that need not be. Nothing to change here."
                    ),
                ),
            ),
            Range(
                lower=_MAIN_SHARE_WATCH_EDGE,
                upper=_MAIN_SHARE_ACT_EDGE,
                recommendation=Recommendation(
                    severity=SEVERITY_WATCH,
                    lever=lever(ACTION_INCREASE, "subagent_dispatch"),
                    detail=(
                        "Part of this session's work now runs with more than "
                        "half the window in play. Nothing is broken; watch "
                        "whether the share climbs as the session goes on, and "
                        "hand the next long file-reading task to a subagent."
                    ),
                ),
            ),
            Range(
                lower=_MAIN_SHARE_ACT_EDGE,
                upper=None,
                recommendation=Recommendation(
                    severity=SEVERITY_ACT,
                    lever=lever(ACTION_REDUCE, "main_thread_context"),
                    detail=(
                        "More than a quarter of main-thread calls carry over "
                        "half the window, and every later call in the session "
                        "re-sends that context. Dispatch long file-reading and "
                        "search work to subagents earlier, or start a fresh "
                        "session at the next natural break."
                    ),
                ),
            ),
        ),
    ),
    METRIC_CACHE_READS_PER_WRITE: Metric(
        key=METRIC_CACHE_READS_PER_WRITE,
        measurement=(
            "cache-read tokens divided by cache-write tokens over the period, "
            "both scopes -- how many times each stored prefix is read back on "
            "average. A period-level aggregate over many prefixes, not "
            "per-prefix arithmetic. Undefined, and so unmeasured, when no call "
            "wrote cache"
        ),
        # DELIBERATELY DOES NOT NAME A BREAK-EVEN. "repays on the first reuse"
        # is true of the 5-minute write and false of the 1-hour one (TA-8), and
        # this flat ratio is precisely the reading that cannot say which -- the
        # slogan TA-8 warns against, and the thing the metric below it exists
        # to call. So the sentence says only what holds either way: reuse is
        # what pays the markup back.
        means=(
            "How many tokens are read back for each token stored. Storing adds "
            "a markup that only reuse pays back, so higher is better."
        ),
        unit=METRIC_UNIT_RATIO,
        # A RATIO OF TWO TOKEN SUMS, so the floor is judged and says so. The
        # members counted are the calls that WROTE cache, which are the calls
        # that put anything in the denominator: a period of a thousand calls
        # where one wrote cache has one contributing call, and its single write
        # sets the whole reading.
        sample=Sample(
            counts="calls that wrote cache",
            rule=FLOOR_RULE_JUDGED,
            minimum=RATIO_SAMPLE_FLOOR,
            provenance=RATIO_SAMPLE_FLOOR_PROVENANCE,
        ),
        worse_when=WORSE_WHEN_LOWER,
        ranges=(
            Range(
                lower=Boundary(0.0, _FLOOR_OF_A_RATIO),
                upper=_READS_PER_WRITE_UNREPAID_EDGE,
                recommendation=Recommendation(
                    severity=SEVERITY_ACT,
                    lever=lever(ACTION_INCREASE, "prompt_prefix_stability"),
                    detail=(
                        "Under one read per write: the stored prefix is being "
                        "thrown away as fast as it is built, so the write "
                        "markup -- 1.25x base input tokens on a five-minute "
                        "TTL, 2x on a one-hour one -- is not repaid either way "
                        "(TA-8). Keep the prompt prefix byte-identical between "
                        "calls: a system prompt, tool list or early file read "
                        "that changes invalidates everything stored after it."
                    ),
                ),
            ),
            Range(
                lower=_READS_PER_WRITE_UNREPAID_EDGE,
                upper=_READS_PER_WRITE_REPAID_EDGE,
                recommendation=Recommendation(
                    severity=SEVERITY_WATCH,
                    lever=lever(ACTION_INCREASE, "prompt_prefix_stability"),
                    detail=(
                        "Between the two break-evens: these writes are repaid "
                        "if they asked for the five-minute TTL and are still a "
                        "loss if they asked for the one-hour one (TA-8), and "
                        "one flat total over both TTLs cannot say which. The "
                        "reading beside it, "
                        f"{METRIC_CACHE_WRITE_REPAYMENT_AT_OWN_TTL}, weighs "
                        "each write by the TTL it actually asked for and does "
                        "call it; where that one reads as unmeasured, this "
                        "period was ingested before the split was read and "
                        "cannot be called either way. One more read per write "
                        "settles it in your favour whichever it was."
                    ),
                ),
            ),
            Range(
                lower=_READS_PER_WRITE_REPAID_EDGE,
                upper=_READS_PER_WRITE_HEALTHY_EDGE,
                recommendation=Recommendation(
                    severity=SEVERITY_WATCH,
                    lever=lever(ACTION_INCREASE, "prompt_prefix_stability"),
                    detail=(
                        "Every write is repaid whichever TTL it asked for, but "
                        "the prefix is being rebuilt more often than a long "
                        "session should need. Look for something early in the "
                        "prompt that changes between calls -- a timestamp, a "
                        "shuffled tool list, a file read that lands before the "
                        "stable part."
                    ),
                ),
            ),
            Range(
                lower=_READS_PER_WRITE_HEALTHY_EDGE,
                upper=None,
                recommendation=Recommendation(
                    severity=SEVERITY_OK,
                    lever=None,
                    detail=(
                        "Reuse is working: each stored prefix is read back many "
                        "times before it expires, which is the whole point of "
                        "storing it. Do not change this."
                    ),
                ),
            ),
        ),
    ),
    METRIC_CACHE_WRITE_REPAYMENT_AT_OWN_TTL: Metric(
        key=METRIC_CACHE_WRITE_REPAYMENT_AT_OWN_TTL,
        measurement=(
            "cache-read tokens on the calls whose per-TTL cache-write split "
            "was measured, divided by the read tokens those calls' writes "
            "require to break even -- one read token per five-minute write "
            "token and two per one-hour write token, TA-8's two break-evens "
            "applied one per TTL rather than averaged. 1.0 is break-even. A "
            "period-level aggregate over many prefixes, not per-prefix "
            "arithmetic: a read carries no TTL of its own, so which write it "
            "repaid is not observed. A call with no split, or with only one of "
            "the two TTLs read, is excluded from BOTH sides -- never counted "
            "as five-minute and never as a zero at either TTL. Undefined, and "
            "so unmeasured, when no such call wrote cache, which is true of "
            "every call ingested before the split was read (#84) and so of "
            "most of any database that predates it"
        ),
        # "twice as much reading" is the two break-evens themselves --
        # READ_TOKENS_TO_REPAY_A_1H_WRITE_TOKEN over its 5-minute twin -- and
        # not a rounding of the 2x/1.25x write markups, which are a different
        # pair of numbers.
        means=(
            "The same reuse check, weighted by how long each stored copy was "
            "asked to last. A one-hour copy takes twice as much reading to pay "
            "for itself as a five-minute one."
        ),
        unit=METRIC_UNIT_RATIO,
        # THE SAME SET THE MEASUREMENT NAMES, and pointedly not the one beside
        # it: the calls whose per-TTL split was measured AND that wrote at
        # either TTL. Borrowing `cache_reads_per_write`'s count here would
        # certify this reading on the strength of calls excluded from both its
        # sides -- the #84 defect the third query exists to avoid, arriving
        # through the floor instead of through the numerator.
        sample=Sample(
            counts="calls whose per-TTL cache-write split was measured and that "
            "wrote at either TTL",
            rule=FLOOR_RULE_JUDGED,
            minimum=RATIO_SAMPLE_FLOOR,
            provenance=RATIO_SAMPLE_FLOOR_PROVENANCE,
        ),
        worse_when=WORSE_WHEN_LOWER,
        ranges=(
            Range(
                lower=Boundary(0.0, _FLOOR_OF_A_RATIO),
                upper=_REPAYMENT_BREAK_EVEN_EDGE,
                recommendation=Recommendation(
                    severity=SEVERITY_ACT,
                    lever=lever(ACTION_INCREASE, "prompt_prefix_stability"),
                    detail=(
                        "These writes are not repaid at the break-even of the "
                        "TTL they asked for: a five-minute write is 1.25x base "
                        "input tokens and needs one read token back per write "
                        "token, a one-hour write is 2x and needs two, and this "
                        "period's reads fall short of what its own mix "
                        "requires (TA-8). Keep the prompt prefix "
                        "byte-identical between calls: a system prompt, tool "
                        "list or early file read that changes invalidates "
                        "everything stored after it."
                    ),
                ),
            ),
            Range(
                lower=_REPAYMENT_BREAK_EVEN_EDGE,
                upper=_REPAYMENT_HEALTHY_EDGE,
                recommendation=Recommendation(
                    severity=SEVERITY_WATCH,
                    lever=lever(ACTION_INCREASE, "prompt_prefix_stability"),
                    detail=(
                        "Every write here is repaid at the break-even of the "
                        "TTL it asked for, with the one-hour writes held to "
                        "the two reads they need rather than the one a flat "
                        "ratio would have accepted. The prefix is still being "
                        "rebuilt more often than a long session should need: "
                        "look for something early in the prompt that changes "
                        "between calls -- a timestamp, a shuffled tool list, a "
                        "file read that lands before the stable part."
                    ),
                ),
            ),
            Range(
                lower=_REPAYMENT_HEALTHY_EDGE,
                upper=None,
                recommendation=Recommendation(
                    severity=SEVERITY_OK,
                    lever=None,
                    detail=(
                        "Reuse is an order of magnitude past what these writes "
                        "require at the TTL each one asked for, so even a "
                        "one-hour write -- twice the base input tokens of an "
                        "uncached send -- is earned back many times over. Do "
                        "not change this."
                    ),
                ),
            ),
        ),
    ),
    METRIC_CACHE_WRITE_ONLY_SHARE: Metric(
        key=METRIC_CACHE_WRITE_ONLY_SHARE,
        measurement=(
            "share of cache-writing calls that read nothing back: calls with "
            "cache_write > 0 AND cache_read = 0, over all calls with "
            "cache_write > 0. Undefined, and so unmeasured, when no call wrote "
            "cache"
        ),
        # PER CALL, which is what is measured -- not "a prefix that was never
        # read again", which would be a claim about what later calls did. A
        # call that stored something and read nothing back is one of these
        # whatever happens next, and the first call of a session always is.
        means=(
            "How often a call stored a prefix and read none back. Each of those "
            "paid the write markup and got nothing for it."
        ),
        unit=METRIC_UNIT_SHARE,
        # 51, AND IT IS THE POINT OF #93. Over three calls this share can only
        # be 0, 1/3, 2/3 or 1, so its `ok` band [0, 0.02) is reachable ONLY at
        # exactly zero -- the green verdict a fresh install earned was the
        # observation that something did not happen three times, reported in
        # the voice of a rate. Seventeen times that sample is what the band it
        # is compared against actually requires, and the number is the band's,
        # not anybody's.
        sample=Sample(
            counts="calls that wrote cache",
            rule=FLOOR_RULE_BAND_GRANULARITY,
        ),
        worse_when=WORSE_WHEN_HIGHER,
        ranges=(
            Range(
                lower=Boundary(0.0, _FLOOR_OF_A_SHARE),
                upper=_WRITE_ONLY_WATCH_EDGE,
                recommendation=Recommendation(
                    severity=SEVERITY_OK,
                    lever=None,
                    detail=(
                        "Almost every prefix this project stores is read back "
                        "at least once, so the write markup is buying "
                        "something. Nothing to change here."
                    ),
                ),
            ),
            Range(
                lower=_WRITE_ONLY_WATCH_EDGE,
                upper=_WRITE_ONLY_ACT_EDGE,
                recommendation=Recommendation(
                    severity=SEVERITY_WATCH,
                    lever=lever(ACTION_INCREASE, "prompt_prefix_stability"),
                    detail=(
                        "A small but real share of stored prefixes is never "
                        "read. Usually a session that ends right after its "
                        "first call, or a prefix that changes on the very next "
                        "turn; worth a look if it grows."
                    ),
                ),
            ),
            Range(
                lower=_WRITE_ONLY_ACT_EDGE,
                upper=None,
                recommendation=Recommendation(
                    severity=SEVERITY_ACT,
                    lever=lever(ACTION_REDUCE, "cache_write"),
                    detail=(
                        "More than one stored prefix in ten is never read back, "
                        "and an unread write is markup for nothing -- 1.25x "
                        "the base input tokens on a five-minute TTL and 2x on "
                        "a one-hour one, with no read to repay either (TA-8). "
                        "Either keep the prefix stable long enough to be read, "
                        "or stop marking a prefix that changes every turn."
                    ),
                ),
            ),
        ),
    ),
    METRIC_MAIN_VS_SUBAGENT_TOKENS_PER_REPLY: Metric(
        key=METRIC_MAIN_VS_SUBAGENT_TOKENS_PER_REPLY,
        measurement=(
            "mean total tokens per main-thread API call divided by mean total "
            "tokens per subagent API call over the period. Undefined, and so "
            "unmeasured, unless both scopes have at least one call -- a project "
            "that never dispatched a subagent has no ratio, not a bad one"
        ),
        # "how many TIMES more", because the reading is a multiple: the mean
        # main-thread call over the mean subagent call. "How much more" would
        # read as a difference in tokens, which is a different number.
        means=(
            "How many times more tokens a main-session reply takes than a "
            "subagent reply. The main session carries the whole history; a "
            "subagent starts clean."
        ),
        unit=METRIC_UNIT_RATIO,
        # THE SMALLER OF THE TWO SCOPES, never the pooled count. This metric
        # already refuses when either scope has no call, and that precedent is
        # about the ratio being UNDEFINED rather than UNDER-SAMPLED -- two
        # different absences. The floor is the second one, and it has to bind on
        # both sides: a period of a thousand main-thread calls and one subagent
        # call has a defined ratio whose denominator is one reply.
        sample=Sample(
            counts="calls in whichever of the two scopes ran fewer",
            rule=FLOOR_RULE_JUDGED,
            minimum=RATIO_SAMPLE_FLOOR,
            provenance=RATIO_SAMPLE_FLOOR_PROVENANCE,
        ),
        worse_when=WORSE_WHEN_HIGHER,
        ranges=(
            Range(
                lower=Boundary(0.0, _FLOOR_OF_A_RATIO),
                upper=_REPLY_RATIO_WATCH_EDGE,
                recommendation=Recommendation(
                    severity=SEVERITY_OK,
                    lever=None,
                    detail=(
                        "A main-thread reply and a subagent reply carry about "
                        "the same tokens, so work is already landing where it "
                        "is cheapest to run. Nothing to change here."
                    ),
                ),
            ),
            Range(
                lower=_REPLY_RATIO_WATCH_EDGE,
                upper=_REPLY_RATIO_ACT_EDGE,
                recommendation=Recommendation(
                    severity=SEVERITY_WATCH,
                    lever=lever(ACTION_INCREASE, "subagent_dispatch"),
                    detail=(
                        "A main-thread reply carries appreciably more than a "
                        "subagent one. That is normal for a session that mostly "
                        "coordinates; it is worth watching if the gap widens."
                    ),
                ),
            ),
            Range(
                lower=_REPLY_RATIO_ACT_EDGE,
                upper=None,
                recommendation=Recommendation(
                    severity=SEVERITY_ACT,
                    lever=lever(ACTION_INCREASE, "subagent_dispatch"),
                    detail=(
                        "A main-thread reply carries three times or more what a "
                        "subagent reply does, so the same work runs on far "
                        "fewer tokens dispatched. Move file reading, search and "
                        "long analysis into subagents and keep the main thread "
                        "for decisions."
                    ),
                ),
            ),
        ),
    ),
}


# --------------------------------------------------------------------------
# The one metric whose value this module computes
# --------------------------------------------------------------------------


def cache_write_repayment(
    read_tokens: Optional[int],
    write_5m_tokens: Optional[int],
    write_1h_tokens: Optional[int],
) -> Optional[float]:
    """`METRIC_CACHE_WRITE_REPAYMENT_AT_OWN_TTL`'s value, or `None` if unmeasured.

    Every other metric's value is the caller's arithmetic; this one is not, and
    the exception is deliberate. WHICH NUMBER MULTIPLIES WHICH TTL IS THE CITED
    BOUNDARY. If `serve.py` wrote `2 *` into a query, the weighting and the
    citation that justifies it would be free to drift apart -- the defect
    `serve.RANKED_BY` was introduced to stop one level up, where a heading and
    an `ORDER BY` disagreed for a whole release. So the weights, the boundary
    and this function are one thing in one file, and a caller can only get it
    right.

    Totals, not per-call values: sum each column over the period's calls whose
    split was measured, and pass the reads from THOSE SAME CALLS, so the two
    sides of the ratio range over one set. `ingest.Call.cache_write_split_total`
    is where the same "a partial split is not a total" rule lives on the write
    path.

    `None` -- never a number -- in three cases, three different absences with
    one honest answer:

      * either write total unmeasured. A call with no split is not a call that
        wrote nothing at that TTL: reading an absent 1-hour total as `0` would
        report writes that cost 2x base input tokens as though they cost 1.25x,
        and say they were repaid at a read they were not.
      * reads unmeasured.
      * nothing required -- no measured write token in the period. A zero
        denominator is an unmeasured metric, and returning `inf` (or `0.0`)
        would be exactly the absence-as-a-value `assess()` then refuses.

    A negative input raises: token counts are counts, and a negative one means
    the caller is passing something other than what this signature says.
    """
    if read_tokens is None or write_5m_tokens is None or write_1h_tokens is None:
        return None
    for name, count in (
        ("read_tokens", read_tokens),
        ("write_5m_tokens", write_5m_tokens),
        ("write_1h_tokens", write_1h_tokens),
    ):
        if count < 0:
            raise ValueError(f"{name} cannot be negative: got {count!r}")
    required = (
        READ_TOKENS_TO_REPAY_A_5M_WRITE_TOKEN * write_5m_tokens
        + READ_TOKENS_TO_REPAY_A_1H_WRITE_TOKEN * write_1h_tokens
    )
    if required == 0:
        return None
    return read_tokens / required


# --------------------------------------------------------------------------
# Lookup
# --------------------------------------------------------------------------

# What the caller must render for a metric with no sample. A named constant
# rather than a bare `None` at the call site, because "no recommendation" has
# to be SAID -- an unmeasured metric that simply vanishes from the page is
# indistinguishable from a healthy one, which is the defect this table exists
# to avoid.
UNMEASURED_NOTE = (
    "not measured -- no sample, so no recommendation. This is not the same as "
    "healthy."
)

# The THIRD state's note (#93), and it is deliberately not a variant of the one
# above. "Nobody measured this" and "this was measured over too little to mean
# anything" are different absences with different remedies, and the remedy is
# the one genuinely useful thing the report can tell a new user -- so it is
# said here, in the table's voice, rather than typed into the page.
UNDER_SAMPLED_NOTE = (
    "not enough data yet -- there IS a reading, and this period does not yet "
    "hold enough of what it counts for the table's bands to mean anything over "
    "it. This is not a verdict and it is not a clean bill of health: come back "
    "after a few more sessions, or widen the period. Each row states how many "
    "it has and how many it needs."
)

# The three states a metric can be in for one period. Enumerated, in the order
# "usable" to "least established", so that "every state is handled" is a
# checkable statement rather than a habit -- `CONTEXT_ANSWER_STATES`' rule.
#
# THREE, NOT TWO. A real 0, a metric nobody measured and one measured over too
# small a sample are three different claims. Collapsing the last two is what
# made a fresh install indistinguishable from a healthy corpus (#93); collapsing
# the first two is the defect this module was written for (#78).
SAMPLE_MEASURED = "measured"
SAMPLE_UNMEASURED = "unmeasured"
SAMPLE_UNDER_SAMPLED = "under-sampled"
SAMPLE_STATES = (SAMPLE_MEASURED, SAMPLE_UNDER_SAMPLED, SAMPLE_UNMEASURED)


@dataclass(frozen=True)
class Reading:
    """One metric's raw input: the value, AND how many members its sample had.

    Both, always, and in one object on purpose. `assess_all()` used to take a
    bare `Optional[float]`, which made "how much was this measured over?" a
    question the table had no way to ask -- so the only sample size it could
    react to was zero, and one call bought a verdict. A caller cannot now
    supply a value without saying what it was measured over.

    `sample_size` counts the members of the metric's OWN denominator, which is
    what `Metric.sample.counts` names. It is not the period's call count, and
    the difference is the whole of `SampleFloor.counts`' docstring.

    A number over an empty sample is refused rather than banded: it cannot
    happen -- every one of the five values is None exactly when its denominator
    is empty -- so if it ever arrives, a caller has paired a value with the
    wrong counter, and that is a wrong number that would read right.
    """

    value: Optional[float]
    sample_size: int

    def __post_init__(self) -> None:
        if isinstance(self.sample_size, bool) or not isinstance(self.sample_size, int):
            raise ValueError(
                f"sample_size must be a count, got {self.sample_size!r}"
            )
        if self.sample_size < 0:
            raise ValueError(
                f"sample_size cannot be negative: got {self.sample_size!r}"
            )
        if self.value is not None and self.sample_size == 0:
            raise ValueError(
                f"a value of {self.value!r} over a sample of 0 is arithmetic "
                "over an empty set: the value and the count do not describe "
                "the same rows"
            )


@dataclass(frozen=True)
class Assessment:
    """One metric's verdict, carrying everything the page needs to render it.

    Including both boundaries and both provenances: the page shows the range a
    value fell in, and a reader must be able to see which of its two edges is a
    citation and which is somebody's judgment without leaving the page.
    """

    metric: str
    measurement: str
    value: float
    severity: str
    recommendation: str
    lever: Optional[Lever]
    depth_in_severity: float
    range_lower: float
    range_upper: Optional[float]
    lower_provenance: Provenance
    upper_provenance: Optional[Provenance]


@dataclass(frozen=True)
class UnderSampled:
    """A metric that HAS a reading and not enough sample to band it (#93).

    It carries the value, and that is deliberate: the number was measured and
    is true, so withholding it would be its own small dishonesty. What is
    withheld is the SEVERITY -- there is no verdict, no lever and no directive,
    because those are claims the sample cannot support. A caller rendering this
    must show it as neither `ok` nor unmeasured.

    `shortfall` is derived rather than stored, so it cannot disagree with the
    two numbers it is the difference of.
    """

    metric: str
    measurement: str
    value: float
    sample_size: int
    floor: SampleFloor

    @property
    def shortfall(self) -> int:
        return self.floor.minimum - self.sample_size


@dataclass(frozen=True)
class Assessments:
    """A ranked run over several metrics, plus the two kinds of absence.

    `unmeasured` is not an error list and not an empty result: it is the set of
    metrics whose recommendation is `UNMEASURED_NOTE`, carried so the page can
    say "not measured" where it would otherwise say nothing at all.

    `under_sampled` is the THIRD state and not a flavour of either neighbour.
    Its members have a reading, so they are not unmeasured; they have no
    severity, so they cannot join `ranked` -- a ranking is by severity and then
    by depth into it, and a metric with neither would have to be given one to
    be placed. Three tuples, three states, and every metric lands in exactly
    one of them (`assess_all()` partitions).

    `sample_sizes` is every metric's supplied count, measured or not, keyed by
    metric. It lives on the RUN rather than on `Assessment` because that is what
    it is a property of: how much a period held is a fact about the period, and
    `assess()` remains the pure banding lookup it has always been -- a value
    falls in a range, and how many rows produced the value plays no part in
    WHICH range. Carried at all because a measured row has to be able to show
    "51 of 51": a count published only where it was short would read as an error
    message rather than as the standing condition it is.
    """

    ranked: tuple[Assessment, ...]
    unmeasured: tuple[str, ...]
    under_sampled: tuple[UnderSampled, ...]
    sample_sizes: Mapping[str, int]


def assess(metric_key: str, value: Optional[float]) -> Optional[Assessment]:
    """The entry for `value` under `metric_key`, or `None` if unmeasured.

    `None` in gives `None` out, and that is the load-bearing case: a metric with
    no sample must not fall through to the healthy range, because a page that
    renders "nothing to change here" over a number nobody measured is asserting
    a clean bill of health it never took. `window_for_model()` returning `None`
    for a model it does not cover is the same answer to the same question.

    An unknown `metric_key` raises: that is a caller bug, not an absent
    measurement, and returning `None` would file it under the one meaning
    `None` already carries.

    A non-finite or negative value also raises. `inf` and `nan` are what a
    zero denominator produces, and a zero denominator means unmeasured -- the
    caller must pass `None`, not a number arithmetic invented.
    """
    metric = METRICS.get(metric_key)
    if metric is None:
        raise KeyError(f"no metric named {metric_key!r}")
    if value is None:
        return None
    if not math.isfinite(value):
        raise ValueError(
            f"{metric_key} is {value!r}: a non-finite value is an unmeasured "
            "metric wearing a number -- pass None"
        )
    floor = metric.ranges[0].lower.value
    if value < floor:
        raise ValueError(
            f"{metric_key} cannot be below {floor!r}: got {value!r}, which "
            "means an input is not what this function was told it is"
        )
    entry = metric.range_for(value)
    return Assessment(
        metric=metric.key,
        measurement=metric.measurement,
        value=value,
        severity=entry.recommendation.severity,
        recommendation=entry.recommendation.text,
        lever=entry.recommendation.lever,
        depth_in_severity=metric.depth(value),
        range_lower=entry.lower.value,
        range_upper=None if entry.upper is None else entry.upper.value,
        lower_provenance=entry.lower.provenance,
        upper_provenance=None if entry.upper is None else entry.upper.provenance,
    )


def rank(assessments: Iterable[Assessment]) -> tuple[Assessment, ...]:
    """Worst first: severity, then depth into the severity band, then key.

    Derived, never authored. The table's order is not consulted and neither is
    the caller's dict order -- the same set of assessments produces the same
    sequence however it arrived, which is what #66 needs in order to show "the
    three biggest levers" and mean it. The metric-key tiebreak is arbitrary but
    stable, chosen precisely because it is visibly not authoring order.
    """
    return tuple(
        sorted(
            assessments,
            key=lambda a: (-SEVERITY_RANK[a.severity], -a.depth_in_severity, a.metric),
        )
    )


def sample_state(metric_key: str, reading: Reading) -> str:
    """Which of `SAMPLE_STATES` `reading` is in for `metric_key`.

    THE ORDER OF THE TWO REFUSALS MATTERS. No value at all is `unmeasured`
    whatever the count says -- a metric can have members in its denominator and
    still produce nothing, and reporting that as "not enough data yet" would
    promise the reader that more sessions will fix something the arithmetic
    will not. Only a reading that EXISTS can be under-sampled.

    The floor is compared against the metric's own denominator, which is what
    `Reading.sample_size` is required to carry.
    """
    metric = METRICS.get(metric_key)
    if metric is None:
        raise KeyError(f"no metric named {metric_key!r}")
    if not isinstance(reading, Reading):
        raise TypeError(
            f"{metric_key}: expected a Reading(value, sample_size), got "
            f"{type(reading).__name__}. A bare value cannot say what it was "
            "measured over, which is the whole of #93"
        )
    if reading.value is None:
        return SAMPLE_UNMEASURED
    if reading.sample_size < metric.sample_floor.minimum:
        return SAMPLE_UNDER_SAMPLED
    return SAMPLE_MEASURED


def assess_all(readings: Mapping[str, Reading]) -> Assessments:
    """Assess every metric in `METRICS`, ranked, with both absences named.

    `readings` must have an entry for EVERY metric -- a `Reading` whose value is
    `None` where there is no sample. Omitting a key is refused rather than
    treated as unmeasured: a caller that forgot a metric and a caller that
    measured nothing would otherwise produce the same page, and only one of
    them is telling the truth.

    THE PARTITION IS TOTAL. Every metric lands in exactly one of the three
    tuples, and nothing falls through to `ranked` by default -- which is the
    same guarantee `Metric.range_for()` makes about the value axis, made about
    the sample axis instead.
    """
    missing = sorted(set(METRICS) - set(readings))
    if missing:
        raise KeyError(
            f"no reading supplied for {missing}; pass "
            "Reading(None, <sample size>) to say 'not measured'"
        )
    unknown = sorted(set(readings) - set(METRICS))
    if unknown:
        raise KeyError(f"no metric named {unknown}")
    measured: list[Assessment] = []
    unmeasured: list[str] = []
    under_sampled: list[UnderSampled] = []
    for key in sorted(METRICS):
        reading = readings[key]
        state = sample_state(key, reading)
        if state == SAMPLE_UNMEASURED:
            unmeasured.append(key)
            continue
        if state == SAMPLE_UNDER_SAMPLED:
            assert reading.value is not None  # `sample_state` guarantees it
            under_sampled.append(
                UnderSampled(
                    metric=key,
                    measurement=METRICS[key].measurement,
                    value=reading.value,
                    sample_size=reading.sample_size,
                    floor=METRICS[key].sample_floor,
                )
            )
            continue
        result = assess(key, reading.value)
        # Unreachable: `sample_state` has already refused a None value, and
        # `assess` returns None only for one. Raising beats appending nothing,
        # which would drop a metric out of all three tuples silently.
        if result is None:  # pragma: no cover - defends the partition
            raise RuntimeError(f"{key}: measured state produced no assessment")
        measured.append(result)
    return Assessments(
        ranked=rank(measured),
        unmeasured=tuple(unmeasured),
        under_sampled=tuple(under_sampled),
        sample_sizes={key: readings[key].sample_size for key in sorted(METRICS)},
    )
