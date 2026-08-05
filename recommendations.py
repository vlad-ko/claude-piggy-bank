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
exists rather than a dict literal. Of the thirteen boundaries here, exactly two
are documented facts with a citation -- `1.0` and `2.0` reads per write, where
TA-8's arithmetic puts the two cache-write break-evens. Four are domain floors,
which arithmetic fixes and nobody decided. The remaining seven are product-
owner judgments with no source anywhere. Anthropic publishes the cache
multipliers; it publishes nothing about what share of a window is wasteful and
nothing at all about what a user should do next.

If those sat under one table-level provenance, the judged `0.25` would inherit
the credibility of the cited `1.0` and the page would have no way to tell them
apart. That is exactly the borrowed authority `band_provenance` was introduced
to refuse (#31), reappearing one level down -- so each boundary carries its own
`Provenance`, a citation carries its source and check date and says which fact
it covers, and a judgment structurally CANNOT carry a source (see
`Provenance.__post_init__`).

**Why cache reads-per-write has TWO cited boundaries and a band between them.**
The first draft of this table had one, at `1.0`, citing TA-8 for "a single read
repays the write markup". TA-8 does not say that of every write: it says it of
the **5-minute** write (1.25x, break-even at the first read) and explicitly not
of the **1-hour** write (2x, still a loss at one read, a win at two). It warns
in as many words against compressing either into a slogan.

CPB cannot tell the two apart. `ingest.py` reads `cache_write` from the flat
`cache_creation_input_tokens`; the per-TTL split Claude Code persists beside it
(`usage.cache_creation.ephemeral_5m_input_tokens` /
`ephemeral_1h_input_tokens`) is observed but not ingested, so no column and no
query can separate them today. And the split is not a rounding error: over 400
of this machine's 3,017 transcripts on 2026-08-05, 1-hour writes were
57,887,581 of 221,276,407 cache-write tokens, **26%** -- a raw-record scan, NOT
deduped by `message.id`, so treat it as a share and not as a total.

A single `1.0` cited to TA-8 would therefore be a judged number wearing a cited
one's clothes for a quarter of the corpus. So the two boundaries this table
carries are the two claims that hold **whichever TTL was used**: below `1.0` no
write of either kind is repaid, at or above `2.0` a write of either kind is.
Between them sits the band the data cannot resolve, and it says so in its own
entry rather than picking a side. Ingesting the per-TTL split would let that
band be resolved and is worth filing; until it is, this is the honest shape.

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
    "particular write asked for -- CPB does not record that."
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
    "depends on a TTL CPB does not record.",
    checked="2026-08-04",
    source=_CACHE_SOURCE,
    covers=_CACHE_COVERS,
)

# Measured 2026-08-05 over 400 of this machine's 3,017 transcripts, counting
# `usage.cache_creation` sub-keys on raw records with NO dedupe by
# `message.id`: 57,887,581 of 221,276,407 cache-write tokens asked for the
# 1-hour TTL. A share, not a total -- the streamed-record duplication
# `_dedupe_calls()` exists to remove is still in both numbers. It is recorded
# because it is the evidence that the unresolvable band below is worth having
# rather than a hypothetical: a quarter of this corpus sits on the side of the
# arithmetic a flat 1.0 boundary would get wrong.
ONE_HOUR_WRITE_SHARE_AS_MEASURED = 0.26

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


@dataclass(frozen=True)
class Metric:
    """One measured quantity and the ranges over it.

    `measurement` is the load-bearing field: it names exactly what was divided
    by what, so a reader can check the advice against the number and the two
    cannot drift apart. `serve.py` computing something else under this key is
    then a visible contradiction rather than a silent one.
    """

    key: str
    measurement: str
    worse_when: str
    ranges: tuple[Range, ...]

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
                        "CPB does not record which was requested -- so this "
                        "reading cannot be called either way. One more read "
                        "per write settles it in your favour whichever it was."
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
    METRIC_CACHE_WRITE_ONLY_SHARE: Metric(
        key=METRIC_CACHE_WRITE_ONLY_SHARE,
        measurement=(
            "share of cache-writing calls that read nothing back: calls with "
            "cache_write > 0 AND cache_read = 0, over all calls with "
            "cache_write > 0. Undefined, and so unmeasured, when no call wrote "
            "cache"
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
class Assessments:
    """A ranked run over several metrics, plus the ones with no sample.

    `unmeasured` is not an error list and not an empty result: it is the set of
    metrics whose recommendation is `UNMEASURED_NOTE`, carried so the page can
    say "not measured" where it would otherwise say nothing at all.
    """

    ranked: tuple[Assessment, ...]
    unmeasured: tuple[str, ...]


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


def assess_all(values: Mapping[str, Optional[float]]) -> Assessments:
    """Assess every metric in `METRICS`, ranked, with the unmeasured named.

    `values` must have an entry for EVERY metric -- `None` where there is no
    sample. Omitting a key is refused rather than treated as unmeasured: a
    caller that forgot a metric and a caller that measured nothing would
    otherwise produce the same page, and only one of them is telling the truth.
    """
    missing = sorted(set(METRICS) - set(values))
    if missing:
        raise KeyError(
            f"no value supplied for {missing}; pass None to say 'not measured'"
        )
    unknown = sorted(set(values) - set(METRICS))
    if unknown:
        raise KeyError(f"no metric named {unknown}")
    measured = []
    unmeasured = []
    for key in sorted(METRICS):
        result = assess(key, values[key])
        if result is None:
            unmeasured.append(key)
        else:
            measured.append(result)
    return Assessments(ranked=rank(measured), unmeasured=tuple(unmeasured))
