"""Per-model context windows, and the utilisation bands built on them (#31).

`Avg context/call 266.6k` is a fact with no referent: nothing on the report
says whether that is a lot. Self-comparison cannot supply one -- a percentile
of the reader's own calls compares waste to waste, and says nothing if every
call is wasteful -- so the referent has to be external. Anthropic publishes no
"healthy context size", and inventing one would be this project asserting
something it cannot observe. It DOES publish the context window, which is a
hard limit, and `context_size / window` is a real quantity with a sourced
denominator.

**Why this project has a hand-maintained table again.** It deleted one (#30):
per-MTok list rates, dated by a constant named `RATES_AS_OF`, whose arithmetic
produced a dollar figure that had diverged from real spend by more than 2.5x.
That table's OUTPUT was invented -- it multiplied measured tokens by a list
rate and called the product a cost, while modelling no subscription
accounting, no discount and no overage, so the number was never the thing it
claimed to be. This table's output is a DENOMINATOR. `context_size / window`
is exactly what it says: the share of the model's hard limit that this call
occupied. Nothing is asserted beyond the division.

The staleness stories differ in the same direction, which is what makes this
table safe to hand-maintain where that one was not:

  * a stale rate failed SILENTLY. A rate 2x wrong renders a plausible dollar
    figure, and no reader can see the error from the page;
  * a stale window fails LOUDLY. If a model's window grows and this table does
    not, its calls report utilisation above 100% -- absurd on its face -- so
    `serve.py` counts those calls (`over_window_calls`) and the report can say
    so. The failure announces itself instead of hiding in a plausible number.

That asymmetry is the whole argument. It does not make the table maintenance-
free: `WINDOWS_AS_OF` records when it was last checked and crosses the API so
a reader can judge its currency, exactly as the deleted table's date did.

**Provenance, checked 2026-08-05** against
<https://platform.claude.com/docs/en/about-claude/models/overview>. Covers the
model families in `CONTEXT_WINDOWS` and no others: an id this table does not
name has NO window here, `window_for_model()` returns None, and its
utilisation is INCONCLUSIVE. Never a default, and emphatically never 1M --
that guess is wrong by 5x for Haiku, which is exactly the case a default would
be silently applied to.

**There is no "1M beta" on the current models.** 1M is the default and the
maximum for the 1M-window families listed below: no beta header, no separate
tier, standard billing. So there is no conditional here for one, and adding
one would be modelling a state these models do not have.

**The band boundaries are NOT Anthropic's.** Anthropic publishes the window;
it publishes no guidance that half a window is wasteful. Where the bands sit
is a product-owner judgment dated `BANDS_AS_OF`, and it is carried across the
API as such (`band_provenance`) beside the window's own, separate provenance
(`window_provenance`). Merging the two would lend a documented fact's
authority to a judged one, which is the borrowed authority this repository
exists to refuse.

(The deleted rate table is described here rather than named: `tests/
test_serve.py` asserts that no shipped module mentions it, and that guard is
worth keeping total.)
"""

from __future__ import annotations

from typing import Optional

# Last date `CONTEXT_WINDOWS` was checked against the documentation cited in
# WINDOW_SOURCE. Bump the two together, or the date is a claim about a table
# that no longer exists.
WINDOWS_AS_OF = "2026-08-05"
WINDOW_SOURCE = "https://platform.claude.com/docs/en/about-claude/models/overview"

# Documented context window, in tokens, keyed by model-id PREFIX. Lookup is by
# LONGEST matching prefix with a `-` boundary (or an exact match), so
# `claude-opus-5` cannot match a hypothetical `claude-opus-5x` -- a family
# boundary is precisely where a window is free to change.
#
# Haiku is why this is per-model rather than one constant. Measured 2026-08-05
# on the reference corpus: its largest call carries 111,700 tokens, which is
# 55.9% of its 200K window and 11.2% of a wrongly assumed 1M one -- a 5x
# misread that moves the call across two band boundaries.
CONTEXT_WINDOWS: dict[str, int] = {
    "claude-opus-5": 1_000_000,
    "claude-opus-4-8": 1_000_000,
    "claude-opus-4-7": 1_000_000,
    "claude-opus-4-6": 1_000_000,
    "claude-sonnet-5": 1_000_000,
    "claude-sonnet-4-6": 1_000_000,
    "claude-fable-5": 1_000_000,
    "claude-haiku-4-5": 200_000,
}

WINDOW_PROVENANCE = (
    f"documented context window, checked {WINDOWS_AS_OF} against {WINDOW_SOURCE}"
)

# When the boundaries below were decided. A SECOND date, deliberately, even
# though it currently equals WINDOWS_AS_OF: re-checking Anthropic's published
# window does not re-decide where 50% sits, and one date for both would say it
# had. They are free to drift apart and must be able to.
BANDS_AS_OF = "2026-08-05"
BAND_PROVENANCE = (
    f"product-owner judgment, {BANDS_AS_OF} -- not an Anthropic recommendation. "
    "Anthropic publishes the context window; it publishes no guidance on what "
    "share of it is wasteful."
)

BAND_AT_LEAST_90 = "at-least-90"
BAND_50_TO_90 = "50-to-90"
BAND_25_TO_50 = "25-to-50"
BAND_UNDER_25 = "under-25"

# (key, label, lower, upper) ordered HIGH TO LOW -- the order the report reads
# them in, so the payload needs no sorting downstream. Each band is half-open
# and LOWER-INCLUSIVE, the same convention `serve.day_bounds()` uses for a day:
# a call at exactly 90% of its window is "probably wrong", not "likely
# wasteful". `upper=None` on the top band is not an omission -- utilisation
# above 100% is reachable and must land somewhere (see the module docstring).
#
# Only the top two bands carry a verdict, and only because the judgement was
# made about them. A call under half its window was not judged at all, so its
# label is a range and nothing more; a word there would invent a verdict
# nobody decided.
BANDS: tuple[tuple[str, str, float, Optional[float]], ...] = (
    (BAND_AT_LEAST_90, "90% or more of the window -- probably wrong", 0.90, None),
    (BAND_50_TO_90, "50-90% of the window -- likely wasteful", 0.50, 0.90),
    (BAND_25_TO_50, "25-50% of the window", 0.25, 0.50),
    (BAND_UNDER_25, "under 25% of the window", 0.0, 0.25),
)


def longest_prefix_match(model: str, table: dict[str, int]) -> Optional[int]:
    """`table`'s value for the LONGEST prefix of `model`, or None if none match.

    A prefix matches only at a `-` boundary or exactly, so `claude-opus-4`
    cannot match a hypothetical `claude-opus-45`. Insertion order does not
    decide a tie because there is no tie: the longest match wins outright, and
    two distinct prefixes of the same length cannot both match one id.

    Returns None -- never a default -- for an id no prefix covers.
    """
    best: Optional[str] = None
    for prefix in table:
        if model == prefix or model.startswith(prefix + "-"):
            if best is None or len(prefix) > len(best):
                best = prefix
    return None if best is None else table[best]


def window_for_model(model: str) -> Optional[int]:
    """Documented context window for a model id, or None if it has none here.

    None is the load-bearing answer (CLAUDE.md, "absence is never rendered as a
    value"): a model this table does not cover has no denominator, so it has no
    utilisation, and the caller must count it as INCONCLUSIVE rather than band
    it. Defaulting to 1M would silently file every Haiku call four bands too
    low, and defaulting to anything at all would state a limit nobody checked.
    """
    return longest_prefix_match(model, CONTEXT_WINDOWS)


def band_for(fraction: float) -> str:
    """Which band a utilisation `fraction` (context / window) falls in.

    Lower-inclusive, so the boundary value belongs to the band above it, and
    unbounded at the top so >100% stays in the top band instead of falling off
    the end. A NEGATIVE fraction is impossible from measured tokens against a
    documented window, so it raises rather than being banded as frugal: it
    would mean one of the two inputs is not what this function was told it is.
    """
    if fraction < 0:
        raise ValueError(f"utilisation cannot be negative: {fraction!r}")
    for key, _label, lower, _upper in BANDS:
        if fraction >= lower:
            return key
    # Unreachable: the last band starts at 0.0 and negatives are refused above.
    raise ValueError(f"no band covers {fraction!r}")
