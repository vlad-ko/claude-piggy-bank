"""Tests for `recommendations.py`, the dated recommendation table (#78).

The table's whole value is that it is checkable, so these tests are arranged
around the five ways it could quietly stop being that:

  * a GAP or an OVERLAP between ranges. A gap means some value produces no
    recommendation while the metric was measured perfectly well -- absence
    rendered as absence-shaped silence, which the page cannot distinguish from
    "healthy". Totality is asserted structurally (every range's upper edge IS
    the next range's lower edge) rather than by sampling, and then the
    boundaries themselves and their immediate float neighbours are checked;
  * HEALTHY collapsing into UNMEASURED. Both would render as no advice, and
    only one of them is a statement about the corpus;
  * a JUDGED boundary wearing a CITATION. Three numbers in this table are
    documented -- `1.0` and `2.0` reads per write and `1.0` on the TTL-aware
    ratio, all from TA-8 -- and if the other eight shared their provenance the
    page would present somebody's first draft in the voice of Anthropic's
    documentation;
  * a CITATION applied past what it covers. TA-8 puts the 5-minute write's
    break-even at one read and the 1-hour write's at two. The flat ratio's two
    cited boundaries are therefore stated as the claims that hold EITHER WAY,
    because one total over both TTLs cannot say which applied.
    `CitedBoundaryMatchesTheRecordTest` reads the record and fails if it stops
    saying what is quoted here;
  * the RESOLUTION of the band those two boundaries surround going missing or
    going wrong. Until #84 that band was declared unresolvable and a tripwire
    here asserted the limitation itself -- that `ingest.py` named neither TTL
    key -- so that it would go red the day the limitation lifted. It lifted.
    `TheBandTheTripwireGuardedTest` replaces it and asserts the resolution
    instead: that ingest reads both keys, that the two break-evens are
    TA-8's and are unequal, that a reading inside the old band is now called
    by the period's own TTL mix, and that an unmeasured split is still
    unmeasured rather than assumed to be the cheaper TTL;
  * the ranking coming from the order entries were typed in, which would make
    "the biggest lever" mean "the one written first".

Fixture discipline (CLAUDE.md): the synthetic metrics below give every metric,
every severity and every depth a DIFFERENT value, so a collapsed mapping, a
swapped severity or a depth taken from the wrong end cannot pass by landing on
an equal number. The literal expectations in `BoundariesAreHalfOpenTest` are
written out rather than derived from `METRICS`, because an expectation derived
from the table would move with the table and pin nothing.
"""

from __future__ import annotations

import math
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import recommendations as rec  # noqa: E402
from recommendations import (  # noqa: E402
    ACTION_INCREASE,
    ACTION_REDUCE,
    METRIC_CACHE_READS_PER_WRITE,
    METRIC_CACHE_WRITE_ONLY_SHARE,
    METRIC_CACHE_WRITE_REPAYMENT_AT_OWN_TTL,
    METRIC_MAIN_THREAD_SHARE_OVER_HALF_WINDOW,
    METRIC_MAIN_VS_SUBAGENT_TOKENS_PER_REPLY,
    METRICS,
    PROVENANCE_CITED,
    PROVENANCE_JUDGED,
    PROVENANCE_STRUCTURAL,
    RECOMMENDATIONS_AS_OF,
    SEVERITY_ACT,
    SEVERITY_OK,
    SEVERITY_RANK,
    SEVERITY_WATCH,
    WORSE_WHEN_HIGHER,
    WORSE_WHEN_LOWER,
    Assessment,
    Boundary,
    Metric,
    Provenance,
    Range,
    Recommendation,
    assess,
    assess_all,
    cache_write_repayment,
    cited,
    depth_in_band,
    judged,
    lever,
    rank,
    structural,
)

REPO_ROOT = Path(__file__).resolve().parent.parent

# Every documented boundary in the table, so a fourth citation cannot be added
# without being held to what the other three are held to.
CITED_PROVENANCES = (
    rec.UNREPAID_UNDER_EVERY_TTL,
    rec.REPAID_UNDER_EVERY_TTL,
    rec.REPAID_AT_ITS_OWN_TTL,
)

# This project's own corpus, measured 2026-08-05 and recorded in the issue.
# Kept as one fixture so every test that needs "a real reading" uses the same
# one, and so the numbers the boundaries were drafted around stay visible.
CORPUS_2026_08_05 = {
    METRIC_MAIN_THREAD_SHARE_OVER_HALF_WINDOW: 0.389,
    METRIC_CACHE_READS_PER_WRITE: 55.0,
    # `None`, and NOT a number derived from the other readings. These figures
    # were taken before the per-TTL split was ingested (#84), so no call behind
    # them carries one -- which is the state of every database that has not
    # been re-ingested since, and of every transcript already past
    # `cleanupPeriodDays`. Multiplying 55.0 by the corpus-wide 1-hour share to
    # invent a repayment figure would be a measurement nobody took, in the
    # fixture the rest of this file checks real measurements against.
    METRIC_CACHE_WRITE_REPAYMENT_AT_OWN_TTL: None,
    METRIC_CACHE_WRITE_ONLY_SHARE: 0.007,
    METRIC_MAIN_VS_SUBAGENT_TOKENS_PER_REPLY: 4.0,
}

# #93: HOW MANY MEMBERS each of those readings was taken over.
#
# The VALUES above are measured and are quoted from the issue. These counts are
# NOT -- they were never recorded beside them -- so they are CHOSEN, and saying
# so matters more than the numbers: a sample size invented here and presented as
# a measurement would be the defect this whole module is arranged against, in
# the fixture that checks it.
#
# What they are chosen for: every one clears its metric's floor comfortably, so
# a test about BANDING is not accidentally a test about sampling; and all five
# are DELIBERATELY UNEQUAL, so a floor compared against some other metric's
# count -- the "wrong denominator" mutation -- cannot pass by coincidence.
#
# The repayment metric's is 0 because its value is None: these figures predate
# the per-TTL split, so no call behind them contributed a member.
CORPUS_2026_08_05_SAMPLES = {
    METRIC_MAIN_THREAD_SHARE_OVER_HALF_WINDOW: 12_007,
    METRIC_CACHE_READS_PER_WRITE: 9_311,
    METRIC_CACHE_WRITE_REPAYMENT_AT_OWN_TTL: 0,
    METRIC_CACHE_WRITE_ONLY_SHARE: 8_452,
    METRIC_MAIN_VS_SUBAGENT_TOKENS_PER_REPLY: 3_489,
}

CORPUS_2026_08_05_READINGS = {
    key: rec.Reading(value, CORPUS_2026_08_05_SAMPLES[key])
    for key, value in CORPUS_2026_08_05.items()
}

# The #93 corpus: one session, three replies, no subagents -- what a fresh
# install actually holds, and the state no issue in this project had ever been
# measured against. Every value here is a TRUE reading; not one of them is over
# a sample that can carry a verdict, which is the whole finding.
FIRST_RUN_CORPUS_READINGS = {
    METRIC_MAIN_THREAD_SHARE_OVER_HALF_WINDOW: rec.Reading(0.0, 3),
    METRIC_CACHE_READS_PER_WRITE: rec.Reading(24.0, 3),
    METRIC_CACHE_WRITE_REPAYMENT_AT_OWN_TTL: rec.Reading(None, 0),
    METRIC_CACHE_WRITE_ONLY_SHARE: rec.Reading(0.0, 3),
    METRIC_MAIN_VS_SUBAGENT_TOKENS_PER_REPLY: rec.Reading(None, 0),
}


def synthetic_sample(minimum=1):
    """The smallest sample spec `Metric` will accept, for a synthetic RATIO.

    JUDGED, because the synthetics default to `METRIC_UNIT_RATIO` and the
    band-granularity rule is refused for anything that is not a share -- which
    is the guard (#93), so a helper that routed around it would hide it.

    `minimum=1` by default so a synthetic built to test BANDING is not also
    under-sampled: the floor and the range lookup are two different questions,
    and a helper that entangled them would make every ranking test depend on a
    sample size it never mentions. Tests about the floor pass their own.
    """
    return rec.Sample(
        counts="synthetic contributing calls",
        rule=rec.FLOOR_RULE_JUDGED,
        minimum=minimum,
        provenance=rec.judged("synthetic judged floor", decided="2026-08-07"),
    )


def synthetic_metric(
    key,
    worse_when,
    cuts,
    severities,
    unit=rec.METRIC_UNIT_RATIO,
    sample=None,
):
    """A metric with `cuts` interior boundaries and one severity per range.

    Built the way the real table is built -- adjacent ranges SHARE a boundary
    object -- so a test using it is testing the same shape the module ships.
    That now includes a reader sentence, a unit and a sample floor, because
    `Metric` refuses a metric without any of them: a synthetic that could be
    built with less would be testing a shape the module cannot ship.
    """
    edges = [Boundary(0.0, structural("synthetic floor"))]
    edges += [Boundary(c, judged(f"synthetic cut at {c}")) for c in cuts]
    ranges = []
    for i, severity in enumerate(severities):
        upper = edges[i + 1] if i + 1 < len(edges) else None
        ranges.append(
            Range(
                lower=edges[i],
                upper=upper,
                recommendation=Recommendation(
                    severity=severity,
                    lever=None
                    if severity == SEVERITY_OK
                    else lever(ACTION_INCREASE, "subagent_dispatch"),
                    detail=f"synthetic {key} {severity} detail {i}",
                ),
            )
        )
    return Metric(
        key=key,
        measurement=f"synthetic measurement for {key}, long enough to be real",
        means=f"synthetic reader sentence for {key}.",
        unit=unit,
        worse_when=worse_when,
        ranges=tuple(ranges),
        sample=synthetic_sample() if sample is None else sample,
    )


class RangesPartitionTheDomainTest(unittest.TestCase):
    """Total and non-overlapping, proved from the structure rather than sampled.

    Adjacent ranges must meet at ONE boundary: equal values (no gap, and no
    overlap -- equality is precisely both at once) carrying equal provenance,
    so one edge cannot claim a citation the other does not.
    """

    def test_every_metric_starts_at_its_domain_floor(self):
        for key, metric in METRICS.items():
            with self.subTest(metric=key):
                self.assertEqual(metric.ranges[0].lower.value, 0.0)

    def test_every_metric_ends_unbounded_so_no_value_falls_off_the_top(self):
        # A share of 1.0 is reachable -- every call over half its window -- and
        # is exactly the reading the advice matters most for. A top range
        # closing at 1.0 exclusive would drop it.
        for key, metric in METRICS.items():
            with self.subTest(metric=key):
                self.assertIsNone(metric.ranges[-1].upper)
                self.assertIsNotNone(assess(key, 1.0))

    def test_adjacent_ranges_meet_at_one_boundary_value(self):
        for key, metric in METRICS.items():
            for i, (lower, upper) in enumerate(zip(metric.ranges, metric.ranges[1:])):
                with self.subTest(metric=key, seam=i):
                    self.assertIsNotNone(lower.upper)
                    self.assertEqual(lower.upper.value, upper.lower.value)

    def test_adjacent_ranges_meet_at_one_boundary_PROVENANCE(self):
        for key, metric in METRICS.items():
            for i, (lower, upper) in enumerate(zip(metric.ranges, metric.ranges[1:])):
                with self.subTest(metric=key, seam=i):
                    self.assertEqual(lower.upper.provenance, upper.lower.provenance)

    def test_every_bounded_range_has_positive_width(self):
        for key, metric in METRICS.items():
            for entry in metric.ranges:
                if entry.upper is None:
                    continue
                with self.subTest(metric=key, lower=entry.lower.value):
                    self.assertLess(entry.lower.value, entry.upper.value)

    def test_exactly_one_range_contains_each_boundary_and_its_neighbours(self):
        # The boundaries THEMSELVES, the float immediately below each, the
        # float immediately above, the floor, and values far past the top.
        for key, metric in METRICS.items():
            probes = [0.0, math.nextafter(0.0, math.inf), 1e12]
            for entry in metric.ranges:
                for edge in (entry.lower, entry.upper):
                    if edge is None:
                        continue
                    probes += [
                        edge.value,
                        math.nextafter(edge.value, math.inf),
                        math.nextafter(edge.value, -math.inf),
                    ]
            for probe in probes:
                if probe < 0:
                    continue
                with self.subTest(metric=key, probe=probe):
                    hits = [r for r in metric.ranges if r.contains(probe)]
                    self.assertEqual(
                        len(hits), 1, f"{probe!r} landed in {len(hits)} ranges"
                    )

    def test_a_gap_between_ranges_is_detected(self):
        # The invariant above, run against a table with a deliberate hole
        # between 0.2 and 0.3 -- the proof that the assertions have teeth
        # without editing the shipped table.
        holed = Metric(
            key="holed",
            measurement="synthetic measurement, long enough to be real",
            means="What this synthetic number means.",
            unit=rec.METRIC_UNIT_SHARE,
            sample=synthetic_sample(),
            worse_when=WORSE_WHEN_HIGHER,
            ranges=(
                Range(
                    lower=Boundary(0.0, structural("floor")),
                    upper=Boundary(0.2, judged("cut")),
                    recommendation=Recommendation(SEVERITY_OK, None, "low"),
                ),
                Range(
                    lower=Boundary(0.3, judged("cut")),
                    upper=None,
                    recommendation=Recommendation(
                        SEVERITY_ACT,
                        lever(ACTION_INCREASE, "subagent_dispatch"),
                        "high",
                    ),
                ),
            ),
        )
        self.assertEqual([r for r in holed.ranges if r.contains(0.25)], [])
        with self.assertRaises(ValueError):
            holed.range_for(0.25)

    def test_overlapping_ranges_are_detected(self):
        overlapped = Metric(
            key="overlapped",
            measurement="synthetic measurement, long enough to be real",
            means="What this synthetic number means.",
            unit=rec.METRIC_UNIT_SHARE,
            sample=synthetic_sample(),
            worse_when=WORSE_WHEN_HIGHER,
            ranges=(
                Range(
                    lower=Boundary(0.0, structural("floor")),
                    upper=Boundary(0.4, judged("cut")),
                    recommendation=Recommendation(SEVERITY_OK, None, "low"),
                ),
                Range(
                    lower=Boundary(0.3, judged("cut")),
                    upper=None,
                    recommendation=Recommendation(
                        SEVERITY_ACT,
                        lever(ACTION_INCREASE, "subagent_dispatch"),
                        "high",
                    ),
                ),
            ),
        )
        self.assertEqual(
            len([r for r in overlapped.ranges if r.contains(0.35)]),
            2,
            "the containment probe cannot see an overlap it is meant to catch",
        )


class BoundariesAreHalfOpenTest(unittest.TestCase):
    """`lower <= v < upper`, pinned with LITERAL values.

    These expectations are written out rather than read from `METRICS`, which
    is the point: moving any boundary changes what the page says, and one of
    these goes red. A test that derived its expectation from the table would
    move with it and assert nothing.
    """

    PINNED = (
        # (metric, value, severity)
        (METRIC_MAIN_THREAD_SHARE_OVER_HALF_WINDOW, 0.0, SEVERITY_OK),
        (METRIC_MAIN_THREAD_SHARE_OVER_HALF_WINDOW, 0.099, SEVERITY_OK),
        (METRIC_MAIN_THREAD_SHARE_OVER_HALF_WINDOW, 0.10, SEVERITY_WATCH),
        (METRIC_MAIN_THREAD_SHARE_OVER_HALF_WINDOW, 0.249, SEVERITY_WATCH),
        (METRIC_MAIN_THREAD_SHARE_OVER_HALF_WINDOW, 0.25, SEVERITY_ACT),
        (METRIC_MAIN_THREAD_SHARE_OVER_HALF_WINDOW, 1.0, SEVERITY_ACT),
        (METRIC_CACHE_READS_PER_WRITE, 0.0, SEVERITY_ACT),
        (METRIC_CACHE_READS_PER_WRITE, 0.999, SEVERITY_ACT),
        (METRIC_CACHE_READS_PER_WRITE, 1.0, SEVERITY_WATCH),
        (METRIC_CACHE_READS_PER_WRITE, 9.999, SEVERITY_WATCH),
        (METRIC_CACHE_READS_PER_WRITE, 10.0, SEVERITY_OK),
        (METRIC_CACHE_READS_PER_WRITE, 1000.0, SEVERITY_OK),
        # The resolved metric. 1.0 is break-even at whichever TTL the period's
        # writes actually asked for, so the value either side of it is a
        # verdict rather than a band -- the pinning that fails if the 1-hour
        # break-even (2.0 reads per 1-hour write token) is ever moved onto this
        # normalised scale by mistake.
        (METRIC_CACHE_WRITE_REPAYMENT_AT_OWN_TTL, 0.0, SEVERITY_ACT),
        (METRIC_CACHE_WRITE_REPAYMENT_AT_OWN_TTL, 0.999, SEVERITY_ACT),
        (METRIC_CACHE_WRITE_REPAYMENT_AT_OWN_TTL, 1.0, SEVERITY_WATCH),
        (METRIC_CACHE_WRITE_REPAYMENT_AT_OWN_TTL, 1.999, SEVERITY_WATCH),
        (METRIC_CACHE_WRITE_REPAYMENT_AT_OWN_TTL, 9.999, SEVERITY_WATCH),
        (METRIC_CACHE_WRITE_REPAYMENT_AT_OWN_TTL, 10.0, SEVERITY_OK),
        (METRIC_CACHE_WRITE_REPAYMENT_AT_OWN_TTL, 1000.0, SEVERITY_OK),
        (METRIC_CACHE_WRITE_ONLY_SHARE, 0.0, SEVERITY_OK),
        (METRIC_CACHE_WRITE_ONLY_SHARE, 0.019, SEVERITY_OK),
        (METRIC_CACHE_WRITE_ONLY_SHARE, 0.02, SEVERITY_WATCH),
        (METRIC_CACHE_WRITE_ONLY_SHARE, 0.099, SEVERITY_WATCH),
        (METRIC_CACHE_WRITE_ONLY_SHARE, 0.10, SEVERITY_ACT),
        (METRIC_CACHE_WRITE_ONLY_SHARE, 1.0, SEVERITY_ACT),
        (METRIC_MAIN_VS_SUBAGENT_TOKENS_PER_REPLY, 0.0, SEVERITY_OK),
        (METRIC_MAIN_VS_SUBAGENT_TOKENS_PER_REPLY, 1.499, SEVERITY_OK),
        (METRIC_MAIN_VS_SUBAGENT_TOKENS_PER_REPLY, 1.5, SEVERITY_WATCH),
        (METRIC_MAIN_VS_SUBAGENT_TOKENS_PER_REPLY, 2.999, SEVERITY_WATCH),
        (METRIC_MAIN_VS_SUBAGENT_TOKENS_PER_REPLY, 3.0, SEVERITY_ACT),
        (METRIC_MAIN_VS_SUBAGENT_TOKENS_PER_REPLY, 100.0, SEVERITY_ACT),
    )

    # The one seam in the table whose two sides share a severity: the second
    # cited break-even. Both sides are `watch`; what changes is what the page
    # SAYS, so it is pinned on the words rather than on the severity.
    PINNED_PROSE = (
        # The flat ratio's band. It still cannot be called FROM THIS NUMBER --
        # one total over both TTLs -- but it no longer blames CPB for not
        # recording the TTL, and it names the reading that does call it.
        (
            METRIC_CACHE_READS_PER_WRITE,
            1.0,
            METRIC_CACHE_WRITE_REPAYMENT_AT_OWN_TTL,
        ),
        (
            METRIC_CACHE_READS_PER_WRITE,
            1.999,
            "one flat total over both TTLs cannot say which",
        ),
        (METRIC_CACHE_READS_PER_WRITE, 2.0, "repaid whichever TTL"),
        (METRIC_CACHE_READS_PER_WRITE, 9.999, "repaid whichever TTL"),
        # The resolved metric says which side of break-even the period sits on
        # and never hedges between the two TTLs.
        (
            METRIC_CACHE_WRITE_REPAYMENT_AT_OWN_TTL,
            0.999,
            "not repaid at the break-even of the TTL they asked for",
        ),
        (
            METRIC_CACHE_WRITE_REPAYMENT_AT_OWN_TTL,
            1.0,
            "repaid at the break-even of the TTL it asked for",
        ),
    )

    def test_each_pinned_value_gets_its_pinned_severity(self):
        for metric, value, severity in self.PINNED:
            with self.subTest(metric=metric, value=value):
                self.assertEqual(assess(metric, value).severity, severity)

    def test_each_pinned_value_gets_its_pinned_advice(self):
        for metric, value, phrase in self.PINNED_PROSE:
            with self.subTest(metric=metric, value=value):
                self.assertIn(phrase, assess(metric, value).recommendation)

    def test_the_value_on_a_boundary_belongs_to_the_range_above_it(self):
        # `>=` turned into `>` moves every one of these into the range below.
        for key, metric in METRICS.items():
            for entry in metric.ranges:
                edge = entry.lower.value
                with self.subTest(metric=key, boundary=edge):
                    self.assertIs(metric.range_for(edge), entry)

    def test_the_float_below_a_boundary_belongs_to_the_range_below_it(self):
        for key, metric in METRICS.items():
            for below, above in zip(metric.ranges, metric.ranges[1:]):
                edge = above.lower.value
                with self.subTest(metric=key, boundary=edge):
                    self.assertIs(
                        metric.range_for(math.nextafter(edge, -math.inf)), below
                    )

    def test_every_seam_changes_what_the_page_says(self):
        # A boundary whose two sides give identical advice is not a boundary,
        # and a fixture that allowed one would hide a collapsed mapping. Two
        # ranges MAY share a severity (the cache-TTL band does) -- what they
        # may not share is the recommendation.
        for key, metric in METRICS.items():
            for below, above in zip(metric.ranges, metric.ranges[1:]):
                with self.subTest(metric=key, boundary=above.lower.value):
                    self.assertNotEqual(
                        below.recommendation.text, above.recommendation.text
                    )

    def test_the_severity_sequence_of_each_metric_is_the_one_intended(self):
        # Literal, so a collapsed or reordered table is a red test rather than
        # a quieter page.
        self.assertEqual(
            {
                key: tuple(r.recommendation.severity for r in metric.ranges)
                for key, metric in METRICS.items()
            },
            {
                METRIC_MAIN_THREAD_SHARE_OVER_HALF_WINDOW: (
                    SEVERITY_OK,
                    SEVERITY_WATCH,
                    SEVERITY_ACT,
                ),
                METRIC_CACHE_READS_PER_WRITE: (
                    SEVERITY_ACT,
                    SEVERITY_WATCH,
                    SEVERITY_WATCH,
                    SEVERITY_OK,
                ),
                # Three ranges, not four: the resolved metric has no band to
                # split its `watch` in two, which is what resolving the band
                # MEANS. A fourth range here would be the hedge coming back.
                METRIC_CACHE_WRITE_REPAYMENT_AT_OWN_TTL: (
                    SEVERITY_ACT,
                    SEVERITY_WATCH,
                    SEVERITY_OK,
                ),
                METRIC_CACHE_WRITE_ONLY_SHARE: (
                    SEVERITY_OK,
                    SEVERITY_WATCH,
                    SEVERITY_ACT,
                ),
                METRIC_MAIN_VS_SUBAGENT_TOKENS_PER_REPLY: (
                    SEVERITY_OK,
                    SEVERITY_WATCH,
                    SEVERITY_ACT,
                ),
            },
        )

    def test_todays_corpus_lands_where_the_issue_says_it_does(self):
        # `None` is a landing too, and the one this corpus takes on the
        # resolved metric: its readings predate the split, so that metric is
        # unmeasured rather than healthy. Asserted as `None` in the same dict
        # as the four severities so a fixture that quietly acquired a number
        # for it would have to say where the number came from.
        got = {
            key: None if value is None else assess(key, value).severity
            for key, value in CORPUS_2026_08_05.items()
        }
        self.assertEqual(
            got,
            {
                METRIC_MAIN_THREAD_SHARE_OVER_HALF_WINDOW: SEVERITY_ACT,
                METRIC_CACHE_READS_PER_WRITE: SEVERITY_OK,
                METRIC_CACHE_WRITE_REPAYMENT_AT_OWN_TTL: None,
                METRIC_CACHE_WRITE_ONLY_SHARE: SEVERITY_OK,
                METRIC_MAIN_VS_SUBAGENT_TOKENS_PER_REPLY: SEVERITY_ACT,
            },
        )


class HealthyIsAnExplicitEntryTest(unittest.TestCase):
    """"Nothing to change here" is a positive statement and is stored as one."""

    def healthy(self, metric):
        return next(
            r for r in metric.ranges if r.recommendation.severity == SEVERITY_OK
        )

    def test_every_metric_has_exactly_one_healthy_range(self):
        for key, metric in METRICS.items():
            with self.subTest(metric=key):
                healthy = [
                    r for r in metric.ranges if r.recommendation.severity == SEVERITY_OK
                ]
                self.assertEqual(len(healthy), 1)

    def test_the_healthy_entry_carries_prose_of_its_own(self):
        for key, metric in METRICS.items():
            with self.subTest(metric=key):
                self.assertTrue(self.healthy(metric).recommendation.text.strip())

    def test_the_healthy_entry_pulls_no_lever(self):
        for key, metric in METRICS.items():
            with self.subTest(metric=key):
                self.assertIsNone(self.healthy(metric).recommendation.lever)

    def test_a_healthy_entry_that_also_told_you_to_act_is_refused(self):
        with self.assertRaises(ValueError):
            Recommendation(
                severity=SEVERITY_OK,
                lever=lever(ACTION_INCREASE, "subagent_dispatch"),
                detail="all good, but change something",
            )

    def test_a_firing_entry_with_nothing_to_change_is_refused(self):
        for severity in (SEVERITY_WATCH, SEVERITY_ACT):
            with self.subTest(severity=severity), self.assertRaises(ValueError):
                Recommendation(severity=severity, lever=None, detail="be worried")

    def test_every_firing_entry_in_the_table_names_its_lever(self):
        for key, metric in METRICS.items():
            for entry in metric.ranges:
                if entry.recommendation.severity == SEVERITY_OK:
                    continue
                with self.subTest(metric=key, lower=entry.lower.value):
                    self.assertIsNotNone(entry.recommendation.lever)
                    self.assertIn(
                        entry.recommendation.lever.directive,
                        entry.recommendation.text,
                    )


class UnmeasuredIsNotHealthyTest(unittest.TestCase):
    """The rule the whole repository is built on, at this table's boundary."""

    def test_an_unmeasured_metric_yields_no_assessment(self):
        for key in METRICS:
            with self.subTest(metric=key):
                self.assertIsNone(assess(key, None))

    def test_unmeasured_does_not_fall_through_to_the_healthy_range(self):
        for key, metric in METRICS.items():
            healthy = next(
                r for r in metric.ranges if r.recommendation.severity == SEVERITY_OK
            )
            with self.subTest(metric=key):
                self.assertIsNot(assess(key, None), healthy)
                self.assertNotEqual(assess(key, None), healthy.recommendation)

    def test_assess_all_names_the_unmeasured_metrics_rather_than_dropping_them(self):
        values = dict(CORPUS_2026_08_05_READINGS)
        # A value of None over the SAME sample the reading had. "Nobody
        # measured it" and "too few to band it" are the two states this test
        # has to keep apart, and a count of 0 here would reach `unmeasured` by
        # the sample rather than by the absent value -- passing for the wrong
        # reason, which is the mutation `sample_state`'s ordering exists for.
        values[METRIC_CACHE_READS_PER_WRITE] = rec.Reading(
            None, CORPUS_2026_08_05_SAMPLES[METRIC_CACHE_READS_PER_WRITE]
        )
        values[METRIC_MAIN_VS_SUBAGENT_TOKENS_PER_REPLY] = rec.Reading(
            None, CORPUS_2026_08_05_SAMPLES[METRIC_MAIN_VS_SUBAGENT_TOKENS_PER_REPLY]
        )
        result = assess_all(values)
        # Three, not two: the corpus fixture is already unmeasured on the
        # resolved cache metric, which is the state of every database ingested
        # before the per-TTL split was read.
        self.assertEqual(
            result.unmeasured,
            (
                METRIC_CACHE_READS_PER_WRITE,
                METRIC_CACHE_WRITE_REPAYMENT_AT_OWN_TTL,
                METRIC_MAIN_VS_SUBAGENT_TOKENS_PER_REPLY,
            ),
        )
        self.assertEqual(
            sorted(a.metric for a in result.ranked),
            [
                METRIC_CACHE_WRITE_ONLY_SHARE,
                METRIC_MAIN_THREAD_SHARE_OVER_HALF_WINDOW,
            ],
        )

    def test_the_unmeasured_note_says_it_is_not_a_clean_bill_of_health(self):
        self.assertIn("not measured", rec.UNMEASURED_NOTE)
        self.assertIn("not the same as", rec.UNMEASURED_NOTE)

    def test_every_metric_unmeasured_gives_an_empty_ranking_not_an_ok_one(self):
        result = assess_all({key: rec.Reading(None, 0) for key in METRICS})
        self.assertEqual(result.ranked, ())
        self.assertEqual(result.unmeasured, tuple(sorted(METRICS)))
        # And nothing lands in the third state either: an empty corpus is
        # UNMEASURED, not under-sampled. #93's own finding was that the page
        # already handled n = 0 impeccably and n = 3 wrongly, so a change that
        # relabelled the empty case would have moved the defect rather than
        # fixed it.
        self.assertEqual(result.under_sampled, ())

    def test_a_metric_omitted_from_the_input_is_refused_not_assumed(self):
        # The refusal must be `assess_all`'s own and must say what to pass
        # instead. A bare dict lookup raises `KeyError` too, and a test that
        # accepted that would pass over a function which had stopped checking
        # -- which is exactly what a mutation of the guard showed it doing.
        values = dict(CORPUS_2026_08_05_READINGS)
        del values[METRIC_CACHE_READS_PER_WRITE]
        with self.assertRaises(KeyError) as caught:
            assess_all(values)
        message = str(caught.exception)
        self.assertIn(METRIC_CACHE_READS_PER_WRITE, message)
        self.assertIn("no reading supplied", message)
        self.assertIn("Reading(None, <sample size>)", message)

    def test_an_unknown_metric_key_raises_rather_than_returning_none(self):
        # `None` already means "measured nothing". Giving it a second meaning
        # would file a caller's typo under a clean answer.
        with self.assertRaises(KeyError):
            assess("main_thread_share", 0.5)
        with self.assertRaises(KeyError):
            assess_all(
                {**CORPUS_2026_08_05_READINGS, "invented_metric": rec.Reading(1.0, 99)}
            )


class RefusesValuesThatAreNotMeasurementsTest(unittest.TestCase):
    def test_a_non_finite_value_is_refused_rather_than_banded(self):
        # inf is what a caller gets from dividing by a zero denominator, and a
        # zero denominator is an unmeasured metric. Banding it as the worst
        # range would render absence as a value.
        for value in (math.inf, -math.inf, math.nan):
            with self.subTest(value=value), self.assertRaises(ValueError):
                assess(METRIC_CACHE_READS_PER_WRITE, value)

    def test_a_negative_value_is_refused_for_every_metric(self):
        for key in METRICS:
            with self.subTest(metric=key), self.assertRaises(ValueError):
                assess(key, -0.001)

    def test_a_real_zero_is_assessed_not_refused(self):
        # 0.0 is a healthy sample and must stay distinguishable from no sample.
        for key in METRICS:
            with self.subTest(metric=key):
                result = assess(key, 0.0)
                self.assertIsNotNone(result)
                self.assertEqual(result.value, 0.0)


class BoundaryProvenanceTest(unittest.TestCase):
    """One provenance per boundary, and the kinds must not blur.

    Two boundaries in this table are documented. If a judged one shared their
    provenance, the page would render a first draft in the documentation's
    voice -- the borrowed authority `band_provenance` exists to refuse (#31),
    one level down.
    """

    def boundaries(self):
        seen = []
        for key, metric in METRICS.items():
            for entry in metric.ranges:
                for edge in (entry.lower, entry.upper):
                    if edge is not None and not any(edge is s[1] for s in seen):
                        seen.append((key, edge))
        return seen

    def test_every_boundary_carries_a_provenance(self):
        for key, edge in self.boundaries():
            with self.subTest(metric=key, boundary=edge.value):
                self.assertIsInstance(edge.provenance, Provenance)
                self.assertTrue(edge.provenance.statement.strip())

    def test_exactly_three_boundaries_in_the_whole_table_are_cited(self):
        # Three since #84: the flat ratio's two claims that hold whichever TTL
        # was used, and the break-even of the ratio that weights each write by
        # the TTL it asked for. Nothing else in the table is documented.
        cited_edges = sorted(
            (key, edge.value)
            for key, edge in self.boundaries()
            if edge.provenance.kind == PROVENANCE_CITED
        )
        self.assertEqual(
            cited_edges,
            [
                (METRIC_CACHE_READS_PER_WRITE, 1.0),
                (METRIC_CACHE_READS_PER_WRITE, 2.0),
                (METRIC_CACHE_WRITE_REPAYMENT_AT_OWN_TTL, 1.0),
            ],
        )

    def test_the_judged_boundaries_are_the_expected_eight(self):
        judged_edges = sorted(
            (key, edge.value)
            for key, edge in self.boundaries()
            if edge.provenance.kind == PROVENANCE_JUDGED
        )
        self.assertEqual(
            judged_edges,
            sorted(
                [
                    (METRIC_MAIN_THREAD_SHARE_OVER_HALF_WINDOW, 0.10),
                    (METRIC_MAIN_THREAD_SHARE_OVER_HALF_WINDOW, 0.25),
                    (METRIC_CACHE_READS_PER_WRITE, 10.0),
                    (METRIC_CACHE_WRITE_REPAYMENT_AT_OWN_TTL, 10.0),
                    (METRIC_CACHE_WRITE_ONLY_SHARE, 0.02),
                    (METRIC_CACHE_WRITE_ONLY_SHARE, 0.10),
                    (METRIC_MAIN_VS_SUBAGENT_TOKENS_PER_REPLY, 1.5),
                    (METRIC_MAIN_VS_SUBAGENT_TOKENS_PER_REPLY, 3.0),
                ]
            ),
        )

    def test_the_domain_floors_are_structural_and_nothing_else_is(self):
        for key, edge in self.boundaries():
            with self.subTest(metric=key, boundary=edge.value):
                if edge.value == 0.0:
                    self.assertEqual(edge.provenance.kind, PROVENANCE_STRUCTURAL)
                else:
                    self.assertNotEqual(edge.provenance.kind, PROVENANCE_STRUCTURAL)

    def test_the_cited_boundaries_and_the_judged_ones_share_no_provenance(self):
        by_kind = {}
        for key, edge in self.boundaries():
            by_kind.setdefault(edge.provenance.kind, set()).add(
                edge.provenance.statement
            )
        self.assertEqual(
            set(by_kind),
            {PROVENANCE_CITED, PROVENANCE_JUDGED, PROVENANCE_STRUCTURAL},
        )
        # Three cited boundaries, three DIFFERENT statements: three claims
        # about the same two TTLs, not one fact used three times.
        self.assertEqual(len(by_kind[PROVENANCE_CITED]), 3)
        self.assertTrue(
            by_kind[PROVENANCE_CITED].isdisjoint(by_kind[PROVENANCE_JUDGED])
        )

    def test_no_judged_boundary_carries_a_source(self):
        for key, edge in self.boundaries():
            if edge.provenance.kind == PROVENANCE_JUDGED:
                with self.subTest(metric=key, boundary=edge.value):
                    self.assertIsNone(edge.provenance.source)

    def test_every_judged_boundary_is_dated(self):
        for key, edge in self.boundaries():
            if edge.provenance.kind == PROVENANCE_JUDGED:
                with self.subTest(metric=key, boundary=edge.value):
                    self.assertEqual(edge.provenance.checked, RECOMMENDATIONS_AS_OF)

    def test_a_cited_boundarys_check_date_is_its_sources_not_the_tables(self):
        # Deliberately unequal: re-reading the cache documentation does not
        # re-decide where 0.25 sits, and one date for both would say it had.
        # The boundary that resolved the band carries the SAME source date as
        # the two it sits between -- the record did not change when CPB's
        # ability to apply it did, and dating it 2026-08-05 would say TA-8 was
        # re-checked when only ingest.py was.
        for provenance in CITED_PROVENANCES:
            with self.subTest(statement=provenance.statement[:30]):
                self.assertEqual(provenance.checked, "2026-08-04")
                self.assertNotEqual(provenance.checked, RECOMMENDATIONS_AS_OF)

    def test_the_cited_boundaries_state_what_they_do_and_do_not_cover(self):
        for provenance in CITED_PROVENANCES:
            with self.subTest(statement=provenance.statement[:30]):
                self.assertIn("5-minute", provenance.covers)
                self.assertIn("1-hour", provenance.covers)
                self.assertIn(rec.CACHE_ARITHMETIC_ENTRY, provenance.covers)
                # The load-bearing exclusion: the multipliers are documented,
                # which TTL any write asked for is not recorded by CPB.
                self.assertIn("does NOT cover", provenance.covers)

    def test_the_flat_ratios_cited_boundaries_hold_under_both_TTLs(self):
        # The correction this table exists to survive: "one read repays the
        # write" is true of the 5-minute write ONLY. The flat ratio ranges over
        # both TTLs at once whatever ingest records, so its two boundaries must
        # stay phrased as claims about both.
        for provenance in (rec.UNREPAID_UNDER_EVERY_TTL, rec.REPAID_UNDER_EVERY_TTL):
            with self.subTest(statement=provenance.statement[:30]):
                self.assertIn("whichever TTL", provenance.statement)

    def test_the_resolved_boundary_claims_the_opposite_and_says_which(self):
        # The one boundary that is NOT a claim about both at once: it holds
        # each write to its own TTL, which is what the split made possible.
        # "whichever TTL" here would be the hedge wearing the resolution's
        # name.
        statement = rec.REPAID_AT_ITS_OWN_TTL.statement
        self.assertIn("the TTL each one asked for", statement)
        self.assertNotIn("whichever TTL", statement)
        self.assertIn("one read token per 5-minute write token", statement)
        self.assertIn("two per 1-hour write token", statement)

    def test_the_resolved_boundary_records_that_it_used_to_be_a_hedge(self):
        # A boundary that is cited where it was previously a hedge has to say
        # so, or the page shows a citation with no sign that the number it
        # replaced was refused for a year of this table's life.
        statement = rec.REPAID_AT_ITS_OWN_TTL.statement
        self.assertIn("HEDGE", statement)
        self.assertIn("unresolvable band", statement)
        self.assertIn("#84", statement)
        # And the reason must be the one that is true: the record did not
        # change, the measurement did.
        self.assertIn("Neither break-even moved", statement)

    def test_the_flat_band_names_the_reading_that_now_calls_it(self):
        # What replaced "CPB does not record which TTL": the flat number still
        # cannot be called from itself, and the entry now says what can call
        # it instead of blaming a limitation that has lifted.
        band = assess(METRIC_CACHE_READS_PER_WRITE, 1.5).recommendation
        self.assertIn("five-minute", band)
        self.assertIn("one-hour", band)
        self.assertIn("one flat total over both TTLs cannot say which", band)
        self.assertIn(METRIC_CACHE_WRITE_REPAYMENT_AT_OWN_TTL, band)
        self.assertNotIn("does not record which", band)

    def test_no_entry_still_claims_CPB_cannot_see_the_TTL_a_write_asked_for(self):
        # The retired claim, guarded the way the limitation itself used to be.
        # It was true until #84 and is false now, and a false sentence in a
        # provenance is worse than a missing one -- it is the exact shape this
        # module exists to refuse, pointing the other way.
        for text in (
            [rec.RECOMMENDATION_PROVENANCE]
            + [m.measurement for m in METRICS.values()]
            + [r.recommendation.text for m in METRICS.values() for r in m.ranges]
            + [p.statement for p in CITED_PROVENANCES]
            + [p.covers for p in CITED_PROVENANCES]
        ):
            for retired in (
                "CPB does not record",
                "does not record which",
                "CPB cannot",
                "not ingested",
            ):
                with self.subTest(retired=retired, text=text[:40]):
                    self.assertNotIn(retired, text)

    def test_the_measured_one_hour_share_is_recorded_with_its_caveat(self):
        # The evidence for both decisions: that the share is a quarter is why
        # a single 1.0 boundary would have been wrong, and that it is
        # concentrated is why the resolution weights each corpus's own mix
        # rather than this constant.
        self.assertGreater(rec.ONE_HOUR_WRITE_SHARE_AS_MEASURED, 0.0)
        self.assertLess(rec.ONE_HOUR_WRITE_SHARE_AS_MEASURED, 1.0)
        source = " ".join(
            (REPO_ROOT / "recommendations.py").read_text(encoding="utf-8").split()
        )
        # Now a per-CALL figure: the raw-record share it replaced is named as
        # superseded rather than quietly dropped, because it is quoted in the
        # issue this table was drafted from.
        self.assertIn("DEDUPED by `message.id`", source)
        self.assertIn("170,079 calls", source)
        self.assertIn("supersedes an earlier raw-record scan", source)
        self.assertIn("41 of the 3,021 files", source)

    def test_the_flat_totals_understatement_is_recorded_with_its_direction(self):
        # It is why the resolved metric divides by the split and never by the
        # flat total. A count with no direction would not say which way the
        # flat ratio is wrong, and the direction is the whole argument: an
        # understated denominator reads HEALTHIER than the writes justify.
        self.assertGreater(rec.FLAT_WRITE_UNDERSTATED_CALLS_AS_MEASURED, 0)
        self.assertLess(
            rec.FLAT_WRITE_UNDERSTATED_CALLS_AS_MEASURED,
            rec.DEDUPED_CALLS_AS_MEASURED,
        )
        source = " ".join(
            (REPO_ROOT / "recommendations.py").read_text(encoding="utf-8").split()
        )
        self.assertIn("the flat total UNDERSTATED the write", source)
        self.assertIn("the reverse occurred 0 times", source.lower())
        self.assertIn("read HIGHER, i.e. healthier", source)

    def test_the_table_level_provenance_is_judged_and_dated(self):
        self.assertIn(RECOMMENDATIONS_AS_OF, rec.RECOMMENDATION_PROVENANCE)
        self.assertIn("judgment", rec.RECOMMENDATION_PROVENANCE)
        self.assertIn("not an Anthropic recommendation", rec.RECOMMENDATION_PROVENANCE)


class CitedBoundaryMatchesTheRecordTest(unittest.TestCase):
    """The citations resolve. A reference to a renumbered section cites nothing."""

    @classmethod
    def setUpClass(cls):
        cls.record = (REPO_ROOT / rec.TOKEN_ACCOUNTING_RECORD).read_text(
            encoding="utf-8"
        )
        heading = f"## {rec.CACHE_ARITHMETIC_ENTRY} "
        start = cls.record.index(heading)
        end = cls.record.index("\n## ", start + 1)
        cls.section = cls.record[start:end]

    def test_the_record_this_boundary_cites_exists_and_still_has_that_entry(self):
        self.assertTrue((REPO_ROOT / rec.TOKEN_ACCOUNTING_RECORD).exists())
        self.assertIn(f"## {rec.CACHE_ARITHMETIC_ENTRY} ", self.record)

    def test_the_cited_source_url_is_the_one_that_entry_names(self):
        for provenance in (rec.UNREPAID_UNDER_EVERY_TTL, rec.REPAID_UNDER_EVERY_TTL):
            with self.subTest(statement=provenance.statement[:30]):
                self.assertIn(provenance.source, self.section)

    def test_the_multipliers_and_arithmetic_quoted_here_are_the_ones_recorded(self):
        quoted = (
            rec.UNREPAID_UNDER_EVERY_TTL.statement
            + rec.REPAID_UNDER_EVERY_TTL.statement
        )
        for number in ("1.25", "1.35", "2.00", "2.20", "3.00"):
            with self.subTest(number=number):
                self.assertIn(number, self.section)
                self.assertIn(number, quoted)

    def test_the_record_still_says_the_first_read_is_the_five_minute_break_even(self):
        # If this changes upstream, the 1.0 boundary is wrong and this is where
        # it has to be noticed.
        self.assertIn("Break-even is the first read", self.section)

    def test_the_record_still_says_the_one_hour_write_needs_two_reads(self):
        self.assertIn("The 1-hour write is the one that needs **two**", self.section)

    def test_the_record_still_warns_against_the_slogan_this_table_avoided(self):
        self.assertIn(
            'Stating either as "repays on the second hit"', self.section
        )

    def test_the_record_states_the_two_break_evens_this_table_weights_by(self):
        # The resolved boundary's arithmetic is TA-8's two break-evens in
        # another unit. If the record ever stops working them out, the
        # weighting below is uncited and this is where that has to be noticed.
        self.assertIn("Break-even is the first read", self.section)
        self.assertIn("The 1-hour write is the one that needs **two**", self.section)
        self.assertEqual(rec.READ_TOKENS_TO_REPAY_A_5M_WRITE_TOKEN, 1.0)
        self.assertEqual(rec.READ_TOKENS_TO_REPAY_A_1H_WRITE_TOKEN, 2.0)


class TheBandTheTripwireGuardedTest(unittest.TestCase):
    """The band between the two cited boundaries, and how it resolved.

    THIS CLASS REPLACES A TRIPWIRE. Until #84 the test here asserted the
    LIMITATION -- that `ingest.py` mentioned neither per-TTL key -- so that it
    would go red the day the limitation lifted and the band could be resolved
    rather than declared unresolvable. It lifted. A deleted tripwire that
    asserts nothing would be worse than the hedge it stood for, so what is
    pinned now is the resolution: the measurement it rests on, the arithmetic
    it applies, the verdict it produces where the hedge refused one, and the
    absences it still refuses to fill in.

    Fixture discipline: every token count below is hand-built and the two TTLs
    are given DELIBERATELY UNEQUAL values, so a swapped mapping lands on a
    different verdict rather than the same number.
    """

    # One flat reading -- 3,000 read tokens against 2,000 written -- 1.5 reads
    # per write, which is the middle of the old unresolvable band. The three
    # rows differ only in which TTL those 2,000 write tokens asked for, and
    # that alone decides the verdict. This IS the band being resolved.
    READ_TOKENS = 3_000
    SPLITS = (
        # (5m write, 1h write, read tokens required, repayment, severity).
        # Required is `5m x 1 + 1h x 2`, written out per row so the weighting
        # is visible here rather than borrowed from the module under test.
        (2_000, 0, 2_000, 1.5, SEVERITY_WATCH),
        (1_500, 500, 2_500, 1.2, SEVERITY_WATCH),
        (500, 1_500, 3_500, 0.857142857, SEVERITY_ACT),
        (0, 2_000, 4_000, 0.75, SEVERITY_ACT),
    )

    def test_ingest_reads_the_per_TTL_split_the_resolution_rests_on(self):
        # The premise, asserted against ingest's own constants rather than a
        # grep of its source: the resolution is only honest if the split is
        # actually measured per call and stored per call.
        import ingest

        self.assertEqual(ingest.CACHE_WRITE_5M_KEY, "ephemeral_5m_input_tokens")
        self.assertEqual(ingest.CACHE_WRITE_1H_KEY, "ephemeral_1h_input_tokens")
        self.assertNotEqual(ingest.CACHE_WRITE_5M_KEY, ingest.CACHE_WRITE_1H_KEY)
        for column in ("cache_write_5m", "cache_write_1h"):
            with self.subTest(column=column):
                self.assertIn(f"    {column} INTEGER", ingest.SCHEMA)

    def test_the_two_break_evens_are_unequal_which_is_the_whole_difference(self):
        # Collapsed into one constant, this table would be back to averaging
        # the two TTLs -- the slogan TA-8 warns against, in code.
        self.assertNotEqual(
            rec.READ_TOKENS_TO_REPAY_A_5M_WRITE_TOKEN,
            rec.READ_TOKENS_TO_REPAY_A_1H_WRITE_TOKEN,
        )
        self.assertLess(
            rec.READ_TOKENS_TO_REPAY_A_5M_WRITE_TOKEN,
            rec.READ_TOKENS_TO_REPAY_A_1H_WRITE_TOKEN,
        )

    def test_one_flat_reading_in_the_old_band_gets_four_verdicts_by_TTL_mix(self):
        # The heart of it. Every row has the same 1.5 reads per write, which
        # the flat metric can only call `watch` and refuse to resolve; the
        # resolved metric calls each one, and the calls DIFFER.
        flat = assess(METRIC_CACHE_READS_PER_WRITE, 1.5)
        self.assertEqual(flat.severity, SEVERITY_WATCH)
        for write_5m, write_1h, required, expected, severity in self.SPLITS:
            with self.subTest(write_5m=write_5m, write_1h=write_1h):
                self.assertEqual(write_5m + write_1h, 2_000)
                self.assertAlmostEqual(self.READ_TOKENS / required, expected)
                value = cache_write_repayment(self.READ_TOKENS, write_5m, write_1h)
                self.assertAlmostEqual(value, expected)
                result = assess(METRIC_CACHE_WRITE_REPAYMENT_AT_OWN_TTL, value)
                self.assertEqual(result.severity, severity)
        self.assertEqual(
            {row[-1] for row in self.SPLITS},
            {SEVERITY_WATCH, SEVERITY_ACT},
            "a fixture where every mix produced one verdict would pin nothing",
        )

    def test_the_verdict_turns_on_which_TTL_not_on_how_much_was_written(self):
        # An all-1-hour period needs exactly twice the reads an all-5-minute
        # one needs. Stated as the ratio between the two, so a mutation that
        # moved BOTH break-evens together still fails.
        all_5m = cache_write_repayment(self.READ_TOKENS, 2_000, 0)
        all_1h = cache_write_repayment(self.READ_TOKENS, 0, 2_000)
        self.assertAlmostEqual(all_5m / all_1h, 2.0)
        self.assertEqual(
            cache_write_repayment(self.READ_TOKENS * 2, 0, 2_000),
            all_5m,
        )

    def test_an_unmeasured_split_stays_unmeasured_rather_than_five_minute(self):
        # The constraint a default would break: a call with no split is not a
        # call whose writes were all the cheaper TTL, and reading it that way
        # would report 2x writes as repaid at a read they were not.
        for write_5m, write_1h in ((None, None), (2_000, None), (None, 2_000)):
            with self.subTest(write_5m=write_5m, write_1h=write_1h):
                self.assertIsNone(
                    cache_write_repayment(self.READ_TOKENS, write_5m, write_1h)
                )
        self.assertIsNone(cache_write_repayment(None, 2_000, 0))
        # And unmeasured must reach the page as unmeasured, not as healthy.
        self.assertIsNone(assess(METRIC_CACHE_WRITE_REPAYMENT_AT_OWN_TTL, None))

    def test_a_period_that_wrote_no_measured_cache_has_no_ratio(self):
        # A zero denominator is an unmeasured metric, not `inf` and not 0.0 --
        # and 0.0 would be the WORST range on a metric where lower is worse,
        # so the defaulting failure here is an invented alarm.
        self.assertIsNone(cache_write_repayment(self.READ_TOKENS, 0, 0))

    def test_a_real_zero_read_count_is_a_measurement_and_is_kept(self):
        # The other half: a period that wrote cache and read none back is a
        # real 0.0, must stay distinguishable from no sample, and is the
        # worst reading this metric has rather than an absent one.
        value = cache_write_repayment(0, 2_000, 500)
        self.assertEqual(value, 0.0)
        self.assertEqual(
            assess(METRIC_CACHE_WRITE_REPAYMENT_AT_OWN_TTL, value).severity,
            SEVERITY_ACT,
        )

    def test_a_negative_token_count_is_refused_rather_than_divided(self):
        for args in ((-1, 2_000, 0), (3_000, -1, 0), (3_000, 0, -1)):
            with self.subTest(args=args), self.assertRaises(ValueError):
                cache_write_repayment(*args)

    def test_the_resolved_metric_calls_it_and_never_hedges_between_the_TTLs(self):
        # The band is gone from this metric, not merely narrowed: no entry may
        # say it cannot decide. A re-widening that restored the hedge prose
        # fails here even if the boundaries themselves looked untouched.
        metric = METRICS[METRIC_CACHE_WRITE_REPAYMENT_AT_OWN_TTL]
        self.assertEqual(len(metric.ranges), 3)
        cited_edges = [
            edge.value
            for entry in metric.ranges
            for edge in (entry.lower, entry.upper)
            if edge is not None and edge.provenance.kind == PROVENANCE_CITED
        ]
        self.assertEqual(sorted(set(cited_edges)), [1.0])
        for entry in metric.ranges:
            with self.subTest(lower=entry.lower.value):
                text = entry.recommendation.text
                for hedge in ("cannot be called", "either way", "whichever TTL"):
                    self.assertNotIn(hedge, text)

    def test_the_flat_ratio_survived_the_resolution_and_still_has_its_band(self):
        # It is not replaced. Every call ingested before #84 reads NULL for the
        # split and a transcript past `cleanupPeriodDays` can never be
        # re-ingested to fix that, so on most databases the flat ratio is the
        # only one of the two that can be computed at all.
        metric = METRICS[METRIC_CACHE_READS_PER_WRITE]
        self.assertEqual(len(metric.ranges), 4)
        self.assertEqual(
            [entry.lower.value for entry in metric.ranges], [0.0, 1.0, 2.0, 10.0]
        )
        self.assertIn(
            "cannot be called either way",
            assess(METRIC_CACHE_READS_PER_WRITE, 1.5).recommendation,
        )

    def test_the_two_cache_metrics_are_not_the_same_measurement_twice(self):
        # Same corpus, different sets: one ranges over every call, the other
        # only over calls whose split was measured. A page showing both must be
        # able to say so, so each names its own set.
        flat = METRICS[METRIC_CACHE_READS_PER_WRITE].measurement
        resolved = METRICS[METRIC_CACHE_WRITE_REPAYMENT_AT_OWN_TTL].measurement
        self.assertNotEqual(flat, resolved)
        self.assertIn("cache-write tokens over the period", flat)
        self.assertIn("whose per-TTL cache-write split was measured", resolved)
        self.assertIn("excluded from BOTH sides", resolved)


class ProvenanceConstructorsTest(unittest.TestCase):
    """The kinds cannot be blurred by construction, not merely by convention."""

    def test_a_judged_provenance_cannot_carry_a_source(self):
        with self.assertRaises(ValueError):
            Provenance(
                kind=PROVENANCE_JUDGED,
                statement="looks documented",
                checked="2026-08-05",
                source="https://example.invalid/",
            )

    def test_a_cited_provenance_must_name_a_source(self):
        with self.assertRaises(ValueError):
            Provenance(
                kind=PROVENANCE_CITED,
                statement="trust me",
                checked="2026-08-05",
                covers="everything",
            )

    def test_a_cited_provenance_must_say_what_it_covers(self):
        with self.assertRaises(ValueError):
            Provenance(
                kind=PROVENANCE_CITED,
                statement="a fact",
                checked="2026-08-05",
                source="https://example.invalid/",
            )

    def test_a_cited_provenance_must_carry_the_date_it_was_checked(self):
        with self.assertRaises(ValueError):
            cited(
                "a fact",
                checked="",
                source="https://example.invalid/",
                covers="one model",
            )

    def test_a_check_date_must_be_an_iso_date(self):
        with self.assertRaises(ValueError):
            judged("decided whenever", decided="last summer")

    def test_a_structural_provenance_carries_neither_source_nor_date(self):
        with self.assertRaises(ValueError):
            Provenance(
                kind=PROVENANCE_STRUCTURAL,
                statement="arithmetic",
                checked="2026-08-05",
            )
        with self.assertRaises(ValueError):
            Provenance(
                kind=PROVENANCE_STRUCTURAL,
                statement="arithmetic",
                source="https://example.invalid/",
            )

    def test_an_unknown_provenance_kind_is_refused(self):
        with self.assertRaises(ValueError):
            Provenance(kind="vibes", statement="feels right", checked="2026-08-05")

    def test_a_provenance_with_no_statement_is_refused(self):
        with self.assertRaises(ValueError):
            structural("   ")

    def test_the_three_constructors_produce_three_different_kinds(self):
        self.assertEqual(
            [
                cited(
                    "f",
                    checked="2026-08-04",
                    source="https://example.invalid/",
                    covers="x",
                ).kind,
                judged("j").kind,
                structural("s").kind,
            ],
            [PROVENANCE_CITED, PROVENANCE_JUDGED, PROVENANCE_STRUCTURAL],
        )


class AdviceToShrinkTheDiscountIsUnrepresentableTest(unittest.TestCase):
    """Cache read is the 0.1x class. Advice to shrink it is wrong at any scale.

    Not "absent from today's strings": the directive is composed from a closed
    registry and the constructor refuses the pairing, so the sentence cannot be
    built. The prose field is checked for the same sentence, so it cannot be
    smuggled past the registry as free text.
    """

    def test_a_reduce_lever_over_cache_read_cannot_be_built(self):
        with self.assertRaises(ValueError) as caught:
            lever(ACTION_REDUCE, "cache_read")
        self.assertIn("discounted", str(caught.exception))

    def test_an_INCREASE_lever_over_cache_read_is_perfectly_legal(self):
        # Deliberately unequal to the case above: a guard that refused every
        # mention of cache_read would pass the test above and be wrong here.
        built = lever(ACTION_INCREASE, "cache_read")
        self.assertEqual(built.action, ACTION_INCREASE)
        self.assertIn("discount", built.directive)

    def test_reducing_a_full_price_class_is_legal(self):
        for target in ("cache_write", "input_tokens", "output_tokens"):
            with self.subTest(target=target):
                self.assertEqual(lever(ACTION_REDUCE, target).target, target)

    def test_the_non_reducible_set_is_derived_from_the_discounted_classes(self):
        self.assertEqual(rec.NON_REDUCIBLE_TARGETS, frozenset({"cache_read"}))
        self.assertTrue(
            rec.NON_REDUCIBLE_TARGETS <= rec.DISCOUNTED_TOKEN_CLASSES,
            "a target may only be protected because it is discounted",
        )

    def test_prose_advising_a_reduction_of_cache_reads_is_refused(self):
        for detail in (
            "Reduce cache reads to save tokens.",
            "You should minimise cache-read tokens.",
            "Fewer cache_read tokens would help.",
        ):
            with self.subTest(detail=detail), self.assertRaises(ValueError):
                Recommendation(
                    severity=SEVERITY_ACT,
                    lever=lever(ACTION_INCREASE, "subagent_dispatch"),
                    detail=detail,
                )

    def test_prose_that_merely_mentions_cache_reads_is_allowed(self):
        # The guard is scoped to one sentence, so legitimate advice that
        # reduces something else and then mentions the discount stays legal.
        built = Recommendation(
            severity=SEVERITY_ACT,
            lever=lever(ACTION_REDUCE, "main_thread_context"),
            detail="Reduce the main session's context. The cache read discount "
            "then applies to a smaller prefix.",
        )
        self.assertIn("cache read", built.detail)

    def test_no_entry_in_the_table_advises_reducing_a_discounted_class(self):
        for key, metric in METRICS.items():
            for entry in metric.ranges:
                built = entry.recommendation.lever
                with self.subTest(metric=key, lower=entry.lower.value):
                    if built is not None:
                        self.assertFalse(
                            built.action == ACTION_REDUCE
                            and built.target in rec.NON_REDUCIBLE_TARGETS
                        )

    def test_an_unknown_lever_target_is_refused(self):
        with self.assertRaises(ValueError):
            lever(ACTION_INCREASE, "vibes")

    def test_an_unknown_lever_action_is_refused(self):
        with self.assertRaises(ValueError):
            lever("delete", "cache_write")


class DepthInBandTest(unittest.TestCase):
    """Depth runs 0 at the better end to 1 at the worse end, both directions.

    The synthetic bands use deliberately unequal widths and offsets so a depth
    computed from the wrong edge, or normalised by the wrong span, produces a
    different number rather than the same one.
    """

    def test_higher_is_worse_runs_zero_at_the_bottom_to_one_at_the_top(self):
        self.assertAlmostEqual(depth_in_band(0.2, 0.2, 0.6, WORSE_WHEN_HIGHER), 0.0)
        self.assertAlmostEqual(depth_in_band(0.3, 0.2, 0.6, WORSE_WHEN_HIGHER), 0.25)
        self.assertAlmostEqual(depth_in_band(0.5, 0.2, 0.6, WORSE_WHEN_HIGHER), 0.75)

    def test_lower_is_worse_runs_the_other_way(self):
        self.assertAlmostEqual(depth_in_band(0.2, 0.2, 0.6, WORSE_WHEN_LOWER), 1.0)
        self.assertAlmostEqual(depth_in_band(0.3, 0.2, 0.6, WORSE_WHEN_LOWER), 0.75)
        self.assertAlmostEqual(depth_in_band(0.5, 0.2, 0.6, WORSE_WHEN_LOWER), 0.25)

    def test_an_unbounded_band_enters_at_depth_zero_where_higher_is_worse(self):
        self.assertAlmostEqual(depth_in_band(4.0, 4.0, None, WORSE_WHEN_HIGHER), 0.0)
        self.assertAlmostEqual(depth_in_band(8.0, 4.0, None, WORSE_WHEN_HIGHER), 0.5)
        self.assertAlmostEqual(depth_in_band(16.0, 4.0, None, WORSE_WHEN_HIGHER), 0.75)

    def test_an_unbounded_band_enters_at_depth_one_where_lower_is_worse(self):
        self.assertAlmostEqual(depth_in_band(4.0, 4.0, None, WORSE_WHEN_LOWER), 1.0)
        self.assertAlmostEqual(depth_in_band(8.0, 4.0, None, WORSE_WHEN_LOWER), 0.5)

    def test_the_two_formulas_agree_at_the_boundary_they_share(self):
        # The bounded band below a cut ends at depth 1; the unbounded band
        # above it starts at depth 0. A discontinuity there would reorder the
        # ranking around a boundary for no reason anyone decided.
        self.assertAlmostEqual(
            depth_in_band(
                math.nextafter(4.0, -math.inf), 1.0, 4.0, WORSE_WHEN_HIGHER
            ),
            1.0,
        )
        self.assertAlmostEqual(depth_in_band(4.0, 4.0, None, WORSE_WHEN_HIGHER), 0.0)

    def test_a_value_outside_the_band_has_no_depth(self):
        for outside in (0.19, 0.6, 12.0):
            with self.subTest(value=outside), self.assertRaises(ValueError):
                depth_in_band(outside, 0.2, 0.6, WORSE_WHEN_HIGHER)

    def test_an_unbounded_band_starting_at_zero_refuses_a_depth(self):
        with self.assertRaises(ValueError):
            depth_in_band(5.0, 0.0, None, WORSE_WHEN_HIGHER)

    def test_an_unknown_direction_is_refused(self):
        with self.assertRaises(ValueError):
            depth_in_band(0.5, 0.0, 1.0, "sideways")

    def test_no_metric_in_the_table_has_an_undepthable_band(self):
        for key, metric in METRICS.items():
            for entry in metric.ranges:
                lower, upper = metric.severity_band(entry)
                with self.subTest(metric=key, band=(lower, upper)):
                    if upper is None:
                        self.assertGreater(lower, 0.0)


class DepthSpansTheSeverityNotTheRangeTest(unittest.TestCase):
    """Two ranges of one severity are one band for depth, and must be.

    `cache_reads_per_write` splits `watch` at the second cited break-even.
    Measured per range, 2.5 would come out deeper into `watch` than 1.5 --
    which is worse -- and ranking would offer the better corpus as the bigger
    lever. This is the regression that made `severity_band()` exist.
    """

    def test_the_two_watch_ranges_of_one_metric_form_one_band(self):
        metric = METRICS[METRIC_CACHE_READS_PER_WRITE]
        bands = {metric.severity_band(r) for r in metric.ranges}
        self.assertEqual(bands, {(0.0, 1.0), (1.0, 10.0), (10.0, None)})

    def test_a_worse_reading_is_deeper_even_across_an_internal_seam(self):
        worse = assess(METRIC_CACHE_READS_PER_WRITE, 1.5)
        better = assess(METRIC_CACHE_READS_PER_WRITE, 2.5)
        self.assertEqual(worse.severity, better.severity)
        self.assertGreater(worse.depth_in_severity, better.depth_in_severity)

    def test_depth_is_monotonic_in_harm_across_every_metrics_whole_domain(self):
        for key, metric in METRICS.items():
            edges = [r.lower.value for r in metric.ranges]
            probes = sorted(
                {0.0}
                | {e for e in edges}
                | {e + 0.001 for e in edges}
                | {e * 1.5 + 0.25 for e in edges}
                | {max(edges) * 4 + 1}
            )
            previous = {}
            for probe in probes:
                result = assess(key, probe)
                seen = previous.get(result.severity)
                if seen is not None:
                    before_value, before_depth = seen
                    with self.subTest(metric=key, probe=probe, after=before_value):
                        if metric.worse_when == WORSE_WHEN_HIGHER:
                            self.assertGreaterEqual(
                                result.depth_in_severity, before_depth
                            )
                        else:
                            self.assertLessEqual(result.depth_in_severity, before_depth)
                previous[result.severity] = (probe, result.depth_in_severity)


class RankingIsDerivedNotAuthoredTest(unittest.TestCase):
    """Severity, then depth, then the metric key. Never a declaration order.

    #66 renders "the biggest levers" from this order, so an order that came
    from where an entry happened to be typed would make that heading a lie.
    """

    def assessment(self, metric, severity, depth):
        return Assessment(
            metric=metric,
            measurement=f"synthetic {metric}",
            value=depth,
            severity=severity,
            recommendation=f"{metric} says {severity}",
            lever=None,
            depth_in_severity=depth,
            range_lower=0.0,
            range_upper=None,
            lower_provenance=structural("floor"),
            upper_provenance=None,
        )

    def test_severity_outranks_depth(self):
        deep_ok = self.assessment("alpha", SEVERITY_OK, 0.99)
        shallow_act = self.assessment("bravo", SEVERITY_ACT, 0.01)
        shallow_watch = self.assessment("charlie", SEVERITY_WATCH, 0.02)
        ordered = rank([deep_ok, shallow_watch, shallow_act])
        self.assertEqual([a.metric for a in ordered], ["bravo", "charlie", "alpha"])

    def test_depth_orders_within_one_severity(self):
        shallow = self.assessment("alpha", SEVERITY_ACT, 0.10)
        deep = self.assessment("bravo", SEVERITY_ACT, 0.90)
        middling = self.assessment("charlie", SEVERITY_ACT, 0.50)
        ordered = rank([shallow, deep, middling])
        self.assertEqual([a.metric for a in ordered], ["bravo", "charlie", "alpha"])

    def test_the_order_does_not_follow_the_order_it_was_given_in(self):
        # The input is deliberately in exactly the WRONG order, so a `rank`
        # that returned its argument untouched fails.
        given = [
            self.assessment("alpha", SEVERITY_OK, 0.99),
            self.assessment("bravo", SEVERITY_WATCH, 0.10),
            self.assessment("charlie", SEVERITY_ACT, 0.10),
        ]
        self.assertNotEqual([a.metric for a in rank(given)], [a.metric for a in given])

    def test_a_full_tie_breaks_on_the_metric_key_not_on_arrival_order(self):
        first = self.assessment("zulu", SEVERITY_ACT, 0.5)
        second = self.assessment("alpha", SEVERITY_ACT, 0.5)
        self.assertEqual([a.metric for a in rank([first, second])], ["alpha", "zulu"])
        self.assertEqual([a.metric for a in rank([second, first])], ["alpha", "zulu"])

    def test_the_severity_ranking_is_explicit_and_ordered(self):
        self.assertLess(SEVERITY_RANK[SEVERITY_OK], SEVERITY_RANK[SEVERITY_WATCH])
        self.assertLess(SEVERITY_RANK[SEVERITY_WATCH], SEVERITY_RANK[SEVERITY_ACT])

    def test_assess_all_ignores_the_callers_dict_order(self):
        forward = dict(CORPUS_2026_08_05_READINGS)
        backward = {
            k: CORPUS_2026_08_05_READINGS[k] for k in reversed(list(forward))
        }
        self.assertNotEqual(list(forward), list(backward))
        self.assertEqual(
            [a.metric for a in assess_all(forward).ranked],
            [a.metric for a in assess_all(backward).ranked],
        )

    def test_todays_corpus_ranks_the_two_firing_metrics_first(self):
        result = assess_all(CORPUS_2026_08_05_READINGS)
        self.assertEqual(
            [a.metric for a in result.ranked],
            [
                METRIC_MAIN_THREAD_SHARE_OVER_HALF_WINDOW,  # act, depth 0.357
                METRIC_MAIN_VS_SUBAGENT_TOKENS_PER_REPLY,  # act, depth 0.250
                METRIC_CACHE_WRITE_ONLY_SHARE,  # ok,  depth 0.350
                METRIC_CACHE_READS_PER_WRITE,  # ok,  depth 0.182
            ],
        )
        # The fifth metric is absent from the ranking because it is UNMEASURED
        # on this corpus, and is named there rather than dropped. A ranking of
        # four over a table of five is only honest if the fifth is said.
        self.assertEqual(result.unmeasured, (METRIC_CACHE_WRITE_REPAYMENT_AT_OWN_TTL,))

    def test_the_ranking_provenance_names_what_the_order_is_made_of(self):
        self.assertIn(RECOMMENDATIONS_AS_OF, rec.RANKING_PROVENANCE)
        self.assertIn("judgment", rec.RANKING_PROVENANCE)
        self.assertIn("severity", rec.RANKING_PROVENANCE)


class AssessmentCarriesItsEvidenceTest(unittest.TestCase):
    """Advice and number cannot drift apart if the entry carries both."""

    def test_every_metric_names_the_measurement_it_keys_on(self):
        for key, metric in METRICS.items():
            with self.subTest(metric=key):
                self.assertTrue(metric.measurement.strip())
                self.assertGreater(len(metric.measurement), 40)

    def test_an_assessment_carries_the_measurement_and_the_value(self):
        result = assess(METRIC_CACHE_READS_PER_WRITE, 55.0)
        self.assertEqual(
            result.measurement, METRICS[METRIC_CACHE_READS_PER_WRITE].measurement
        )
        self.assertEqual(result.value, 55.0)
        self.assertEqual(result.metric, METRIC_CACHE_READS_PER_WRITE)

    def test_an_assessment_carries_both_edges_of_the_range_it_landed_in(self):
        result = assess(METRIC_CACHE_READS_PER_WRITE, 5.0)
        self.assertEqual(result.range_lower, 2.0)
        self.assertEqual(result.range_upper, 10.0)

    def test_an_assessment_carries_a_provenance_for_each_edge_it_reports(self):
        result = assess(METRIC_CACHE_READS_PER_WRITE, 5.0)
        self.assertEqual(result.lower_provenance.kind, PROVENANCE_CITED)
        self.assertEqual(result.upper_provenance.kind, PROVENANCE_JUDGED)
        self.assertNotEqual(result.lower_provenance, result.upper_provenance)

    def test_a_range_between_two_citations_reports_both_of_them_separately(self):
        result = assess(METRIC_CACHE_READS_PER_WRITE, 1.5)
        self.assertEqual(result.lower_provenance.kind, PROVENANCE_CITED)
        self.assertEqual(result.upper_provenance.kind, PROVENANCE_CITED)
        self.assertNotEqual(
            result.lower_provenance.statement, result.upper_provenance.statement
        )

    def test_the_resolved_metric_reports_its_citation_and_its_judgment_apart(self):
        # Its `watch` range is bounded by one of each: a documented break-even
        # below, somebody's first draft above. The page has to be able to tell
        # them apart without leaving the page, which is the whole reason both
        # edges travel with the assessment.
        result = assess(METRIC_CACHE_WRITE_REPAYMENT_AT_OWN_TTL, 1.5)
        self.assertEqual(result.range_lower, 1.0)
        self.assertEqual(result.range_upper, 10.0)
        self.assertEqual(result.lower_provenance.kind, PROVENANCE_CITED)
        self.assertEqual(result.upper_provenance.kind, PROVENANCE_JUDGED)
        self.assertIsNotNone(result.lower_provenance.source)
        self.assertIsNone(result.upper_provenance.source)

    def test_an_unbounded_range_reports_no_upper_edge_rather_than_a_number(self):
        result = assess(METRIC_CACHE_READS_PER_WRITE, 55.0)
        self.assertIsNone(result.range_upper)
        self.assertIsNone(result.upper_provenance)

    def test_every_metrics_measurement_names_what_makes_it_unmeasurable(self):
        # Four of the five are ratios with a denominator that can be zero, and
        # a zero denominator is an unmeasured metric, not a zero. The resolved
        # cache metric has a second way of being unmeasured -- no call carrying
        # a split at all -- and its measurement has to name that one too.
        for key in (
            METRIC_CACHE_READS_PER_WRITE,
            METRIC_CACHE_WRITE_REPAYMENT_AT_OWN_TTL,
            METRIC_CACHE_WRITE_ONLY_SHARE,
            METRIC_MAIN_VS_SUBAGENT_TOKENS_PER_REPLY,
        ):
            with self.subTest(metric=key):
                self.assertIn("unmeasured", METRICS[key].measurement)


class EveryMetricSpeaksToTheReaderTest(unittest.TestCase):
    """#89: a metric says what its number MEANS, beside what it measures.

    The summary's knob rows printed `measurement`, which is a specification --
    "share of main-thread API calls whose context reaches at least half the
    model's documented window (context_window bands 50-to-90 and at-least-90),
    over main-thread calls with a known window". Correct, checkable, and two
    lines of jargon on a row of advice; it was the page's largest remaining
    source of density.

    `means` is the sentence a reader gets instead, and these tests pin the
    three properties that make it safe to have: it cannot be MISSING (a metric
    added later would otherwise ship with no reader copy), it cannot REPLACE
    the measurement it sits beside, and it cannot say the one thing this module
    exists to make unsayable.
    """

    def test_every_metric_carries_a_reader_sentence(self):
        for key, metric in METRICS.items():
            with self.subTest(metric=key):
                self.assertTrue(metric.means.strip())
                self.assertTrue(
                    metric.means.rstrip().endswith("."),
                    "a reader sentence is a sentence",
                )

    def test_a_metric_with_no_reader_sentence_cannot_be_built(self):
        # THE mutation this field exists to survive: a sixth metric added with
        # everything else filled in. It must fail at CONSTRUCTION, not when
        # some page happens to render it -- `import recommendations` is what
        # every caller does, and `import serve` is not.
        with self.assertRaises(TypeError):
            Metric(
                key="k",
                measurement="a measurement, stated at length so it is real",
                unit=rec.METRIC_UNIT_RATIO,
                sample=synthetic_sample(),
                worse_when=WORSE_WHEN_HIGHER,
                ranges=METRICS[METRIC_CACHE_READS_PER_WRITE].ranges,
            )

    def test_a_blank_reader_sentence_is_refused_rather_than_rendered_empty(self):
        # An empty string satisfies "the field exists" and renders as a blank
        # line under the imperative -- a row that says nothing where the others
        # say why. Whitespace is the same defect wearing a character.
        for blank in ("", "   ", "\n"):
            with self.subTest(means=repr(blank)):
                with self.assertRaises(ValueError) as caught:
                    synthetic_metric_with_means(blank)
                self.assertIn("reader sentence", str(caught.exception))

    def test_the_reader_sentence_cannot_advise_shrinking_the_discount(self):
        # The prose guard `Recommendation` runs on its `detail`, over the field
        # that is written in plain English and is therefore likeliest to reach
        # for the slogan. "Reduce your cache reads" is wrong at every scale;
        # making it unrepresentable in ONE field and not the other beside it
        # would be a guard with a door in it.
        with self.assertRaises(ValueError) as caught:
            synthetic_metric_with_means(
                "This counts your prefixes. Reduce cache reads to save tokens."
            )
        self.assertIn("discounted token class", str(caught.exception))

    def test_a_sentence_that_merely_mentions_the_discount_is_allowed(self):
        # Teeth on the guard above: a check that refused every mention would be
        # unusable, and the table would route around it.
        metric = synthetic_metric_with_means(
            "How much a stored prefix is read back. Cache reads are the cheap "
            "class, so more of them is better."
        )
        self.assertIn("Cache reads", metric.means)

    def test_the_reader_sentence_is_not_the_measurement_and_is_shorter(self):
        # Two fields, two jobs. A `means` that repeated the measurement would
        # satisfy every containment check above while changing nothing on the
        # page, which is the cheapest way to look like this was done.
        for key, metric in METRICS.items():
            with self.subTest(metric=key):
                self.assertNotEqual(metric.means, metric.measurement)
                self.assertLess(
                    len(metric.means),
                    len(metric.measurement),
                    "the reader sentence is longer than the specification it "
                    "was supposed to spare the reader",
                )

    def test_no_two_metrics_share_a_reader_sentence(self):
        # One copied sentence describes the wrong number on one of the two rows
        # and nothing would say which.
        sentences = [metric.means for metric in METRICS.values()]
        self.assertEqual(len(set(sentences)), len(METRICS))

    def test_the_measurements_were_not_touched_on_the_way_past(self):
        # `means` is ADDED, never a rename of `measurement` -- the diagnosis
        # card and the disclosure both still state what was divided by what.
        for key, metric in METRICS.items():
            with self.subTest(metric=key):
                self.assertTrue(metric.measurement.strip())
                self.assertGreater(len(metric.measurement), 40)

    def test_the_flat_ratios_sentence_does_not_name_a_break_even(self):
        # THE accuracy correction, pinned. A draft read "repays on the first
        # reuse", which is true of the 5-minute write (1.25x) and false of the
        # 1-hour one (2x) -- TA-8's two break-evens, and this flat ratio is
        # precisely the reading that cannot say which applied. The metric
        # BESIDE it is the one that calls it, and its own sentence is where the
        # two TTLs are named.
        flat = METRICS[METRIC_CACHE_READS_PER_WRITE].means
        for slogan in ("first reuse", "first read", "one read", "break-even"):
            with self.subTest(slogan=slogan):
                self.assertNotIn(slogan, flat.lower())
        resolved = METRICS[METRIC_CACHE_WRITE_REPAYMENT_AT_OWN_TTL].means
        self.assertIn("one-hour", resolved)
        self.assertIn("five-minute", resolved)
        self.assertIn("twice", resolved)

    def test_the_share_metrics_sentences_describe_shares_of_calls(self):
        # Both shares are over CALLS, and both sentences say "how often"
        # rather than naming a quantity of tokens: a sentence that described
        # the wrong denominator would be a claim the measurement contradicts.
        for key in (
            METRIC_MAIN_THREAD_SHARE_OVER_HALF_WINDOW,
            METRIC_CACHE_WRITE_ONLY_SHARE,
        ):
            with self.subTest(metric=key):
                self.assertIn("How often", METRICS[key].means)

    def test_the_window_sentence_says_at_least_half_and_not_more_than_half(self):
        # The bands counted are 50-to-90 and at-least-90, so a call sitting
        # exactly on half IS one of these. "More than half" would exclude it in
        # words while the arithmetic includes it.
        means = METRICS[METRIC_MAIN_THREAD_SHARE_OVER_HALF_WINDOW].means
        self.assertIn("at least half", means)
        self.assertNotIn("more than half", means)


def synthetic_metric_with_means(means):
    """A metric identical to a real one but for its reader sentence."""
    real = METRICS[METRIC_CACHE_READS_PER_WRITE]
    return Metric(
        key="synthetic_reader_copy",
        measurement=real.measurement,
        means=means,
        unit=real.unit,
        sample=synthetic_sample(),
        worse_when=real.worse_when,
        ranges=real.ranges,
    )


class MetricUnitBelongsToTheMetricTest(unittest.TestCase):
    """#89 review, then #89: what KIND of number a reading is, and where it lives.

    The unit shipped as `serve.METRIC_UNITS`, a mapping beside this table, with
    an import-time guard making it total in both directions -- because the
    branch that added it could not edit this module. Its own note said where it
    belonged. It is here now, and the guard's PROPERTY is what had to survive
    the move: a metric with no unit, or with one no formatter exists for,
    reaches a reader as a raw float.

    It survives in a stronger form. The mapping could only fail when something
    imported `serve`; a field on the dataclass cannot be omitted at all, and an
    unrecognised value fails where the metric is written.
    """

    def test_every_metric_declares_a_unit_from_the_vocabulary(self):
        for key, metric in METRICS.items():
            with self.subTest(metric=key):
                self.assertIn(metric.unit, rec.METRIC_UNIT_KINDS)

    def test_shares_and_ratios_are_not_all_one_unit(self):
        # Teeth on the table: a build that answered "ratio" for everything
        # would satisfy the assertion above while printing two of the five
        # readings as multiples of one.
        self.assertEqual(
            {
                METRICS[METRIC_MAIN_THREAD_SHARE_OVER_HALF_WINDOW].unit,
                METRICS[METRIC_CACHE_WRITE_ONLY_SHARE].unit,
            },
            {rec.METRIC_UNIT_SHARE},
        )
        self.assertEqual(
            {
                METRICS[METRIC_CACHE_READS_PER_WRITE].unit,
                METRICS[METRIC_CACHE_WRITE_REPAYMENT_AT_OWN_TTL].unit,
                METRICS[METRIC_MAIN_VS_SUBAGENT_TOKENS_PER_REPLY].unit,
            },
            {rec.METRIC_UNIT_RATIO},
        )

    def test_a_metric_with_no_unit_cannot_be_built(self):
        with self.assertRaises(TypeError):
            Metric(
                key="k",
                measurement="a measurement, stated at length so it is real",
                means="What this number means.",
                sample=synthetic_sample(),
                worse_when=WORSE_WHEN_HIGHER,
                ranges=METRICS[METRIC_CACHE_READS_PER_WRITE].ranges,
            )

    def test_a_unit_outside_the_vocabulary_is_refused(self):
        # THE mutation the import guard used to catch: a unit invented at the
        # table with no formatter behind it, which reaches the page and falls
        # through to the unitless formatter -- `0.3034` again, arriving later
        # through an unhandled member.
        with self.assertRaises(ValueError) as caught:
            synthetic_metric(
                "synthetic_unit",
                WORSE_WHEN_HIGHER,
                cuts=(1.0,),
                severities=(SEVERITY_OK, SEVERITY_ACT),
                unit="furlongs",
            )
        self.assertIn("furlongs", str(caught.exception))

    def test_the_vocabulary_names_each_unit_once(self):
        self.assertEqual(
            len(set(rec.METRIC_UNIT_KINDS)), len(rec.METRIC_UNIT_KINDS)
        )

    def test_the_vocabulary_may_name_a_unit_no_metric_uses_yet(self):
        # `count` has no metric today and is declared anyway: the page carries
        # one formatter per member, and the failure this vocabulary prevents is
        # a unit arriving with no formatter. Pinned so a tidy-up that deleted
        # the unused member has to argue for it.
        self.assertIn(rec.METRIC_UNIT_COUNT, rec.METRIC_UNIT_KINDS)
        self.assertNotIn(
            rec.METRIC_UNIT_COUNT, {metric.unit for metric in METRICS.values()}
        )

    def test_a_metric_with_an_unreadable_direction_is_refused(self):
        # The third field the constructor now checks. `worse_when` decides
        # which way the page's arrow points, and a value nothing can read would
        # reach `aimWord`'s fallback and print "target" beside a boundary that
        # has a side.
        with self.assertRaises(ValueError):
            Metric(
                key="k",
                measurement="a measurement, stated at length so it is real",
                means="What this number means.",
                unit=rec.METRIC_UNIT_RATIO,
                sample=synthetic_sample(),
                worse_when="sideways",
                ranges=METRICS[METRIC_CACHE_READS_PER_WRITE].ranges,
            )

    def test_a_metric_with_no_range_assesses_nothing_and_is_refused(self):
        with self.assertRaises(ValueError):
            Metric(
                key="k",
                measurement="a measurement, stated at length so it is real",
                means="What this number means.",
                unit=rec.METRIC_UNIT_RATIO,
                sample=synthetic_sample(),
                worse_when=WORSE_WHEN_HIGHER,
                ranges=(),
            )


class NoMoneyAnywhereTest(unittest.TestCase):
    """#30. The multipliers here are token multipliers and stay that way."""

    MONEY_NAME_RE = re.compile(
        r"(?:^|_)(?:cost|costs|usd|price|prices|pricing"
        r"|rate|rates|dollar|dollars)(?:_|$)"
    )

    @classmethod
    def setUpClass(cls):
        cls.source = (REPO_ROOT / "recommendations.py").read_text(encoding="utf-8")

    def rendered_strings(self):
        """Every string this module can put in front of a reader."""
        out = [
            rec.UNMEASURED_NOTE,
            rec.RECOMMENDATION_PROVENANCE,
            rec.RANKING_PROVENANCE,
        ]
        for metric in METRICS.values():
            out.append(metric.measurement)
            # The reader sentence is the string most likely to reach for a
            # currency, because it is the one written in plain English about
            # what something is worth. It goes through the same guard.
            out.append(metric.means)
            for entry in metric.ranges:
                out.append(entry.recommendation.text)
                for edge in (entry.lower, entry.upper):
                    if edge is None:
                        continue
                    out += [
                        edge.provenance.statement,
                        edge.provenance.covers or "",
                        edge.provenance.source or "",
                    ]
        return out

    def test_nothing_the_page_can_render_carries_a_currency_marker(self):
        # Checked on the rendered STRINGS rather than the source, because the
        # source legitimately contains a `$` inside a date regex -- and a
        # guard that cannot tell those apart is the kind that gets deleted.
        for text in self.rendered_strings():
            for symbol in ("$", "USD", "dollar", "cent", "per MTok"):
                with self.subTest(symbol=symbol, text=text[:40]):
                    self.assertNotIn(symbol, text)

    def test_the_module_source_names_no_currency_unit(self):
        for symbol in ("USD", "dollar", "cents", "per MTok"):
            with self.subTest(symbol=symbol):
                self.assertNotIn(symbol, self.source)

    def test_no_field_name_in_an_assessment_is_money_shaped(self):
        result = assess(METRIC_CACHE_READS_PER_WRITE, 55.0)
        for field in vars(result):
            with self.subTest(field=field):
                self.assertIsNone(self.MONEY_NAME_RE.search(field))

    def test_no_field_name_in_a_provenance_is_money_shaped(self):
        for field in vars(rec.UNREPAID_UNDER_EVERY_TTL):
            with self.subTest(field=field):
                self.assertIsNone(self.MONEY_NAME_RE.search(field))

    def test_every_multiplier_quoted_is_named_as_a_token_multiplier(self):
        # TA-8 states the multipliers against the base input price; this module
        # may only carry them as multipliers of base input TOKENS.
        self.assertIn("base input tokens", rec.UNREPAID_UNDER_EVERY_TTL.covers)
        for key, metric in METRICS.items():
            for entry in metric.ranges:
                detail = entry.recommendation.detail
                if "1.25x" in detail or "2x" in detail:
                    with self.subTest(metric=key, lower=entry.lower.value):
                        self.assertIn("input tokens", detail)

    def test_the_module_imports_only_the_standard_library(self):
        import ast

        local = {p.stem for p in REPO_ROOT.glob("*.py")}
        tree = ast.parse(self.source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and not node.level:
                names = [(node.module or "").split(".")[0]]
            else:
                continue
            for name in names:
                with self.subTest(name=name):
                    self.assertTrue(
                        name in sys.stdlib_module_names or name in local,
                        f"{name} is neither stdlib nor local",
                    )


class SyntheticTableBehavesLikeTheRealOneTest(unittest.TestCase):
    """The helpers above are exercised on a table nobody has to redline.

    Deliberately unequal everywhere: cuts at three different values, one
    severity per range, and a direction opposite to the first metric's, so a
    swapped direction or a collapsed severity map cannot pass.
    """

    def test_a_synthetic_metric_partitions_its_domain_too(self):
        metric = synthetic_metric(
            "synthetic_higher",
            WORSE_WHEN_HIGHER,
            cuts=(0.3, 0.7),
            severities=(SEVERITY_OK, SEVERITY_WATCH, SEVERITY_ACT),
        )
        landings = [
            metric.range_for(v).recommendation.severity
            for v in (0.0, 0.29, 0.3, 0.69, 0.7, 900.0)
        ]
        self.assertEqual(
            landings,
            [
                SEVERITY_OK,
                SEVERITY_OK,
                SEVERITY_WATCH,
                SEVERITY_WATCH,
                SEVERITY_ACT,
                SEVERITY_ACT,
            ],
        )

    def test_a_synthetic_metric_with_the_opposite_direction_ranks_the_other_way(self):
        metric = synthetic_metric(
            "synthetic_lower",
            WORSE_WHEN_LOWER,
            cuts=(0.3, 0.7),
            severities=(SEVERITY_ACT, SEVERITY_WATCH, SEVERITY_OK),
        )
        self.assertGreater(metric.depth(0.05), metric.depth(0.25))

    def test_a_synthetic_metric_merges_its_own_repeated_severities(self):
        metric = synthetic_metric(
            "synthetic_merged",
            WORSE_WHEN_HIGHER,
            cuts=(0.2, 0.5, 0.8),
            severities=(SEVERITY_OK, SEVERITY_WATCH, SEVERITY_WATCH, SEVERITY_ACT),
        )
        self.assertEqual(
            [metric.severity_band(r) for r in metric.ranges],
            [(0.0, 0.2), (0.2, 0.8), (0.2, 0.8), (0.8, None)],
        )
        self.assertLess(metric.depth(0.3), metric.depth(0.6))


def share_metric(cuts, severities, counts="synthetic members"):
    """A SHARE whose floor the band-granularity rule derives from its own cuts."""
    metric = synthetic_metric(
        "synthetic_share",
        WORSE_WHEN_HIGHER,
        cuts=cuts,
        severities=severities,
        unit=rec.METRIC_UNIT_SHARE,
        sample=rec.Sample(counts=counts, rule=rec.FLOOR_RULE_BAND_GRANULARITY),
    )
    return metric


def smallest_n_the_hard_way(width):
    """`smallest_sample_finer_than` restated as a search from 1.

    Deliberately NOT the module's implementation. That one starts near
    `floor(1/width)` to save iterations; this one grinds up from 1 and can
    therefore agree with it only if the shortcut is right. A test that called
    the module's own arithmetic would be checking that a function equals
    itself.
    """
    n = 1
    while 1.0 / n >= width:
        n += 1
    return n


class SampleFloorIsDerivedFromTheTableTest(unittest.TestCase):
    """#93: how many members a SHARE needs, and that nobody chose the number.

    A share over `n` members moves in steps of `1/n`, so it can express a value
    strictly inside a band only where `1/n` is finer than that band. The floor
    is therefore whatever the metric's own narrowest bounded band requires --
    arithmetic over boundaries the table already carries, moving by itself when
    one of them is redlined.
    """

    def test_the_two_shares_floors_are_eleven_and_fifty_one(self):
        # The numbers themselves, written out, so a change to either is a
        # visible diff rather than a recomputation that agrees with itself.
        self.assertEqual(
            METRICS[METRIC_MAIN_THREAD_SHARE_OVER_HALF_WINDOW].sample_floor.minimum,
            11,
        )
        self.assertEqual(
            METRICS[METRIC_CACHE_WRITE_ONLY_SHARE].sample_floor.minimum, 51
        )

    def test_the_floor_is_computed_from_the_bands_and_not_written_down(self):
        # THE mutation this class exists for: a floor hard-coded to the value
        # the real table happens to produce. A synthetic share with a different
        # narrowest band must get a different floor, and 21 is not 11 or 51.
        self.assertEqual(
            share_metric(
                cuts=(0.05, 0.4), severities=(SEVERITY_OK, SEVERITY_WATCH, SEVERITY_ACT)
            ).sample_floor.minimum,
            21,
        )

    def test_it_is_the_narrowest_bounded_band_that_sets_the_floor(self):
        # Not the first, not the healthy one, not the widest: the NARROWEST. A
        # floor satisfying only a wide band would leave the narrow one
        # reachable at its edges alone, which is exactly the defect --
        # `cache_write_only_share`'s ok band was reachable only at literal zero.
        # The narrow band is put LAST here so an implementation reading
        # `ranges[0]` cannot pass: the first band is 0.5 wide and would give 3.
        #
        # The cuts are chosen to subtract EXACTLY in binary (0.75 - 0.5 = 0.25).
        # That is a property of the fixture, not of the rule: a band of
        # [0.5, 0.54) has a computed width of 0.04000000000000004, and the
        # floor that follows from it is 25 rather than 26 -- correct for the
        # width the table actually holds, and a distraction in a test about
        # WHICH band is read. Both of the real table's narrowest bands start at
        # 0.0, so neither carries that noise.
        metric = share_metric(
            cuts=(0.5, 0.75), severities=(SEVERITY_OK, SEVERITY_WATCH, SEVERITY_ACT)
        )
        self.assertEqual(metric.sample_floor.minimum, 5)

    def test_the_unbounded_top_range_is_not_treated_as_a_band(self):
        # It has no width to be finer than, and inventing a ceiling to give it
        # one is the refusal `depth_in_band()` already makes. A metric whose
        # only bounded band is wide gets a small floor, not an enormous one.
        metric = share_metric(cuts=(0.5,), severities=(SEVERITY_OK, SEVERITY_ACT))
        self.assertEqual(metric.sample_floor.minimum, 3)

    def test_a_step_exactly_equal_to_the_band_is_not_fine_enough(self):
        # The comparison is `>=`, and the boundary case is the whole reason:
        # over ten calls a share moves in steps of 0.1, which reaches the edges
        # of a 0.1-wide band and nothing between them. Eleven is the first
        # sample that can land inside it.
        self.assertEqual(rec.smallest_sample_finer_than(0.1), 11)
        self.assertEqual(rec.smallest_sample_finer_than(0.02), 51)
        # Off by one either side, so the boundary is pinned rather than
        # approached: a rule using `>` would answer 10 and 50 here.
        self.assertLess(1.0 / 11, 0.1)
        self.assertEqual(1.0 / 10, 0.1)

    def test_the_shortcut_start_agrees_with_a_search_from_one(self):
        # `smallest_sample_finer_than` starts near `floor(1/width)` to save
        # iterations, and `floor` on a float is exactly where an off-by-one
        # would hide. Checked against a grind from 1 over widths that include
        # both of the real table's and several that are not representable.
        for width in (0.1, 0.02, 0.05, 0.3, 0.07, 1.0, 1.5, 0.25, 0.0125):
            with self.subTest(width=width):
                self.assertEqual(
                    rec.smallest_sample_finer_than(width),
                    smallest_n_the_hard_way(width),
                )

    def test_a_derived_floor_is_structural_and_carries_no_date(self):
        # There is nothing to re-check: the number moves by itself when a
        # boundary moves, so a date beside it would claim a currency it has
        # not got. `Provenance` refuses a structural boundary with either.
        floor = METRICS[METRIC_CACHE_WRITE_ONLY_SHARE].sample_floor
        self.assertEqual(floor.rule, rec.FLOOR_RULE_BAND_GRANULARITY)
        self.assertEqual(floor.provenance.kind, rec.PROVENANCE_STRUCTURAL)
        self.assertIsNone(floor.provenance.checked)
        self.assertIsNone(floor.provenance.source)
        # And it says the band it came from may itself be a judgment, so a
        # reader is not told the floor is beyond argument when its input is not.
        self.assertIn("judgment", floor.provenance.statement)

    def test_the_derived_statement_quotes_the_band_it_was_derived_from(self):
        # A derivation nobody can follow is an assertion. The sentence carries
        # the band, its width and the resulting floor, so the arithmetic can be
        # checked from the page.
        floor = METRICS[METRIC_CACHE_WRITE_ONLY_SHARE].sample_floor
        statement = floor.provenance.statement
        for fragment in ("[0, 0.02)", "0.02", "50", "51"):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, statement)


class TheRatiosFloorIsJudgedAndSaysSoTest(unittest.TestCase):
    """#93: the three ratios do NOT get the shares' derivation, or their number.

    A ratio of two token sums has no `1/n` step -- one call contributes its own
    token count, which is unbounded. So the argument that derives the shares'
    floor does not apply, and the module refuses to let it be stretched rather
    than trusting anybody to remember.
    """

    RATIOS = (
        METRIC_CACHE_READS_PER_WRITE,
        METRIC_CACHE_WRITE_REPAYMENT_AT_OWN_TTL,
        METRIC_MAIN_VS_SUBAGENT_TOKENS_PER_REPLY,
    )

    def test_every_ratios_floor_is_judged_dated_and_sourceless(self):
        for key in self.RATIOS:
            with self.subTest(metric=key):
                floor = METRICS[key].sample_floor
                self.assertEqual(floor.rule, rec.FLOOR_RULE_JUDGED)
                self.assertEqual(floor.provenance.kind, rec.PROVENANCE_JUDGED)
                self.assertEqual(floor.provenance.checked, rec.SAMPLE_FLOOR_AS_OF)
                # A judgment cannot carry a source -- `Provenance` refuses one
                # -- and this asserts the state rather than the refusal.
                self.assertIsNone(floor.provenance.source)

    def test_the_judged_date_is_not_the_boundaries_date(self):
        # Re-deciding where 0.25 sits does not re-decide how many calls a ratio
        # needs. One date covering both would say that it had.
        self.assertNotEqual(rec.SAMPLE_FLOOR_AS_OF, rec.RECOMMENDATIONS_AS_OF)

    def test_the_statement_says_it_is_a_judgment_and_why_deriving_is_refused(self):
        statement = rec.RATIO_SAMPLE_FLOOR_PROVENANCE.statement
        self.assertIn("JUDGMENT", statement)
        # It has to name what a derivation would have needed, or "judged" is a
        # label rather than an argument.
        self.assertIn("bound on how much of a period's tokens one call may carry",
                      statement)

    def test_the_share_rule_applied_to_a_ratio_returns_a_rubber_stamp(self):
        # The evidence the statement rests on, computed rather than quoted: the
        # band-granularity rule does not FAIL on these metrics, it succeeds and
        # answers 2, 2 and 1. A floor that certifies a two-call sample while
        # wearing arithmetic is worse than one that says it was decided, which
        # is the whole argument for judging them.
        got = {
            key: rec.band_granularity_floor(
                METRICS[key].ranges, "would-be members"
            ).minimum
            for key in self.RATIOS
        }
        self.assertEqual(
            got,
            {
                METRIC_CACHE_READS_PER_WRITE: 2,
                METRIC_CACHE_WRITE_REPAYMENT_AT_OWN_TTL: 2,
                METRIC_MAIN_VS_SUBAGENT_TOKENS_PER_REPLY: 1,
            },
        )
        # And every one of those is below the judged floor actually used, so
        # the refusal is protective rather than decorative.
        for key, would_be in got.items():
            with self.subTest(metric=key):
                self.assertLess(would_be, METRICS[key].sample_floor.minimum)

    def test_a_ratio_cannot_be_given_the_band_granularity_rule_at_all(self):
        # Unrepresentable, not merely discouraged -- `lever()`'s treatment of
        # "reduce your cache reads", one field over. The metric cannot be
        # CONSTRUCTED, so no build can ship one.
        with self.assertRaises(ValueError) as caught:
            synthetic_metric(
                "synthetic_stretched",
                WORSE_WHEN_HIGHER,
                cuts=(0.5,),
                severities=(SEVERITY_OK, SEVERITY_ACT),
                unit=rec.METRIC_UNIT_RATIO,
                sample=rec.Sample(
                    counts="members", rule=rec.FLOOR_RULE_BAND_GRANULARITY
                ),
            )
        self.assertIn("only honest for a", str(caught.exception))
        self.assertIn("steps of 1/n", str(caught.exception))

    def test_the_ratios_do_not_share_the_shares_number(self):
        # The instruction the issue gave in as many words. 10 is neither 11
        # nor 51, and this fails if somebody reaches for whichever is to hand.
        for key in self.RATIOS:
            with self.subTest(metric=key):
                self.assertEqual(
                    METRICS[key].sample_floor.minimum, rec.RATIO_SAMPLE_FLOOR
                )
                self.assertNotIn(METRICS[key].sample_floor.minimum, (11, 51))


class EveryFloorNamesItsOwnDenominatorTest(unittest.TestCase):
    """#93: "51" means nothing until something says 51 of WHAT.

    The floor counts the members of the metric's own denominator, which is not
    the period's call count and is not the same set for any two of these
    metrics.
    """

    def test_every_metric_says_what_its_floor_counts(self):
        for key, metric in METRICS.items():
            with self.subTest(metric=key):
                self.assertTrue(metric.sample_floor.counts.strip())

    def test_a_floor_that_names_nothing_is_refused(self):
        for blank in ("", "   "):
            with self.subTest(counts=repr(blank)):
                with self.assertRaises(ValueError) as caught:
                    rec.Sample(counts=blank, rule=rec.FLOOR_RULE_BAND_GRANULARITY)
                self.assertIn("unnamed denominator", str(caught.exception))

    def test_the_two_cache_metrics_over_one_set_still_need_different_amounts(self):
        # `cache_reads_per_write` and `cache_write_only_share` both count the
        # calls that wrote cache, and need 10 and 51 of them. How many members
        # a reading needs is the TABLE's question, not the query's, so one
        # shared count does not imply one shared floor.
        reads = METRICS[METRIC_CACHE_READS_PER_WRITE].sample_floor
        write_only = METRICS[METRIC_CACHE_WRITE_ONLY_SHARE].sample_floor
        self.assertEqual(reads.counts, write_only.counts)
        self.assertNotEqual(reads.minimum, write_only.minimum)

    def test_the_reply_ratio_counts_the_thinner_scope_not_the_pool(self):
        counts = METRICS[METRIC_MAIN_VS_SUBAGENT_TOKENS_PER_REPLY].sample_floor.counts
        self.assertIn("fewer", counts)

    def test_the_per_ttl_metric_does_not_borrow_the_flat_ratios_set(self):
        # Different sets, so different floors' denominators -- the #84 defect
        # arriving through the sample instead of through the numerator.
        self.assertNotEqual(
            METRICS[METRIC_CACHE_WRITE_REPAYMENT_AT_OWN_TTL].sample_floor.counts,
            METRICS[METRIC_CACHE_READS_PER_WRITE].sample_floor.counts,
        )


class AReadingCarriesItsOwnSampleSizeTest(unittest.TestCase):
    """#93: a value cannot be supplied without saying what it was measured over."""

    def test_a_number_over_an_empty_sample_is_refused(self):
        # It cannot happen -- every one of the five values is None exactly when
        # its denominator is empty -- so if it arrives, a caller has paired a
        # value with the wrong counter. That is a wrong number that reads right,
        # which is the one thing this repository refuses to let pass.
        with self.assertRaises(ValueError) as caught:
            rec.Reading(0.5, 0)
        self.assertIn("do not describe", str(caught.exception))

    def test_a_negative_sample_is_refused(self):
        with self.assertRaises(ValueError):
            rec.Reading(None, -1)

    def test_a_sample_size_that_is_not_a_count_is_refused(self):
        for bad in (1.5, "3", True, None):
            with self.subTest(size=repr(bad)):
                with self.assertRaises(ValueError):
                    rec.Reading(None, bad)

    def test_no_value_over_a_large_sample_is_unmeasured_not_under_sampled(self):
        # THE ORDER OF THE TWO REFUSALS. A metric can have members in its
        # denominator and still produce nothing, and calling that "not enough
        # data yet" would promise the reader more sessions will fix something
        # arithmetic will not.
        self.assertEqual(
            rec.sample_state(METRIC_CACHE_READS_PER_WRITE, rec.Reading(None, 5_000)),
            rec.SAMPLE_UNMEASURED,
        )

    def test_a_bare_float_is_refused_rather_than_read_as_a_reading(self):
        with self.assertRaises(TypeError) as caught:
            rec.sample_state(METRIC_CACHE_READS_PER_WRITE, 3.0)
        self.assertIn("cannot say what it was measured over", str(caught.exception))


class TheThreeStatesAreThreeTest(unittest.TestCase):
    """#93: a real 0, an unmeasured metric and an under-sampled one differ.

    The fresh install is the case: every figure individually correct, and the
    composition asserting a clean bill of health on evidence that cannot
    support one.
    """

    def test_a_first_run_bands_nothing_and_names_every_reason(self):
        result = assess_all(FIRST_RUN_CORPUS_READINGS)
        # Not one verdict. Before #93 all three of these were `ranked`, and
        # `cache_reads_per_write` read "Do not change this." over three calls.
        self.assertEqual(result.ranked, ())
        self.assertEqual(
            [u.metric for u in result.under_sampled],
            [
                METRIC_CACHE_READS_PER_WRITE,
                METRIC_CACHE_WRITE_ONLY_SHARE,
                METRIC_MAIN_THREAD_SHARE_OVER_HALF_WINDOW,
            ],
        )
        self.assertEqual(
            result.unmeasured,
            (
                METRIC_CACHE_WRITE_REPAYMENT_AT_OWN_TTL,
                METRIC_MAIN_VS_SUBAGENT_TOKENS_PER_REPLY,
            ),
        )

    def test_an_under_sampled_reading_keeps_its_value_and_loses_its_verdict(self):
        under = {
            u.metric: u
            for u in assess_all(FIRST_RUN_CORPUS_READINGS).under_sampled
        }
        reading = under[METRIC_CACHE_READS_PER_WRITE]
        # The number was measured and is true, so it survives...
        self.assertEqual(reading.value, 24.0)
        # ...and every claim ABOUT it does not. `UnderSampled` carries no
        # severity, no lever and no recommendation text: there is no field on
        # it that could render as a verdict.
        for banned in ("severity", "lever", "recommendation"):
            with self.subTest(field=banned):
                self.assertFalse(hasattr(reading, banned))

    def test_the_shortfall_is_derived_from_the_two_numbers_it_separates(self):
        under = next(
            u
            for u in assess_all(FIRST_RUN_CORPUS_READINGS).under_sampled
            if u.metric == METRIC_CACHE_WRITE_ONLY_SHARE
        )
        self.assertEqual(under.sample_size, 3)
        self.assertEqual(under.floor.minimum, 51)
        self.assertEqual(under.shortfall, 48)

    def test_one_more_member_crosses_the_floor_and_earns_a_verdict(self):
        # The floor is a floor and not a ban: at exactly its minimum the
        # reading bands. Asserted at the boundary from both sides, because a
        # `<=` where a `<` belongs is invisible anywhere else.
        floor = METRICS[METRIC_CACHE_READS_PER_WRITE].sample_floor.minimum
        self.assertEqual(
            rec.sample_state(
                METRIC_CACHE_READS_PER_WRITE, rec.Reading(24.0, floor - 1)
            ),
            rec.SAMPLE_UNDER_SAMPLED,
        )
        self.assertEqual(
            rec.sample_state(METRIC_CACHE_READS_PER_WRITE, rec.Reading(24.0, floor)),
            rec.SAMPLE_MEASURED,
        )

    def test_the_under_sampled_note_is_not_the_unmeasured_one(self):
        self.assertNotEqual(rec.UNDER_SAMPLED_NOTE, rec.UNMEASURED_NOTE)
        # It says what to do, which is the one genuinely useful thing the
        # report can tell a new user...
        self.assertIn("come back after a few more sessions", rec.UNDER_SAMPLED_NOTE)
        # ...and says plainly that it is not a verdict, so the sentence cannot
        # be read as a mild all-clear.
        self.assertIn("not a clean bill of health", rec.UNDER_SAMPLED_NOTE)

    def test_the_three_states_are_three_distinct_names(self):
        self.assertEqual(len(set(rec.SAMPLE_STATES)), 3)
        self.assertIn(rec.SAMPLE_MEASURED, rec.SAMPLE_STATES)
        self.assertIn(rec.SAMPLE_UNDER_SAMPLED, rec.SAMPLE_STATES)
        self.assertIn(rec.SAMPLE_UNMEASURED, rec.SAMPLE_STATES)

    def test_the_partition_is_total_over_every_metric(self):
        for readings in (FIRST_RUN_CORPUS_READINGS, CORPUS_2026_08_05_READINGS):
            with self.subTest(corpus=sorted(readings)[0]):
                result = assess_all(readings)
                landed = (
                    [a.metric for a in result.ranked]
                    + list(result.unmeasured)
                    + [u.metric for u in result.under_sampled]
                )
                self.assertEqual(sorted(landed), sorted(METRICS))
                self.assertEqual(len(landed), len(set(landed)))

    def test_every_supplied_sample_size_is_published_on_the_run(self):
        result = assess_all(CORPUS_2026_08_05_READINGS)
        self.assertEqual(result.sample_sizes, CORPUS_2026_08_05_SAMPLES)


class SampleSpecRefusesEveryBlurringTest(unittest.TestCase):
    """#93: the two floor rules cannot wear each other's clothes."""

    def test_a_judged_floor_without_a_number_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            rec.Sample(
                counts="members",
                rule=rec.FLOOR_RULE_JUDGED,
                provenance=rec.judged("decided", decided="2026-08-07"),
            )
        self.assertIn("must state its own minimum", str(caught.exception))

    def test_a_judged_floor_below_one_member_is_refused(self):
        # A floor of zero admits a reading over nothing, which is the state the
        # whole change exists to stop being a verdict.
        with self.assertRaises(ValueError):
            rec.Sample(
                counts="members",
                rule=rec.FLOOR_RULE_JUDGED,
                minimum=0,
                provenance=rec.judged("decided", decided="2026-08-07"),
            )

    def test_a_judged_floor_wearing_a_structural_provenance_is_refused(self):
        # The blurring that matters: a decided number presented as arithmetic
        # nobody could have chosen otherwise.
        with self.assertRaises(ValueError) as caught:
            rec.Sample(
                counts="members",
                rule=rec.FLOOR_RULE_JUDGED,
                minimum=10,
                provenance=structural("looks like arithmetic"),
            )
        self.assertIn("has to say so", str(caught.exception))

    def test_a_derived_floor_carrying_its_own_number_is_refused(self):
        # A typed number could drift from the bands it claims to come from,
        # which is the entire property the derivation buys.
        with self.assertRaises(ValueError) as caught:
            rec.Sample(
                counts="members",
                rule=rec.FLOOR_RULE_BAND_GRANULARITY,
                minimum=51,
            )
        self.assertIn("neither number nor", str(caught.exception))

    def test_an_unknown_rule_is_refused(self):
        with self.assertRaises(ValueError):
            rec.Sample(counts="members", rule="whatever-seems-right")

    def test_a_metric_with_no_sample_cannot_be_built(self):
        # `sample` has no default, so a metric whose first call earns it a
        # verdict is not constructible.
        with self.assertRaises(TypeError):
            Metric(
                key="k",
                measurement="a measurement, stated at length so it is real",
                means="What this number means.",
                unit=rec.METRIC_UNIT_RATIO,
                worse_when=WORSE_WHEN_HIGHER,
                ranges=METRICS[METRIC_CACHE_READS_PER_WRITE].ranges,
            )


if __name__ == "__main__":
    unittest.main()
