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
  * a JUDGED boundary wearing a CITATION. Two numbers in this table are
    documented -- `1.0` and `2.0` reads per write, from TA-8 -- and if the
    other seven shared their provenance the page would present somebody's
    first draft in the voice of Anthropic's documentation;
  * a CITATION applied past what it covers. TA-8 puts the 5-minute write's
    break-even at one read and the 1-hour write's at two, and CPB does not
    record which TTL a write asked for. The two cited boundaries here are
    therefore stated as the claims that hold EITHER WAY, and the band between
    them says out loud that it cannot be called. `CitedBoundaryMatchesTheRecord
    Test` reads the record and fails if it stops saying what is quoted here;
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
    cited,
    depth_in_band,
    judged,
    lever,
    rank,
    structural,
)

REPO_ROOT = Path(__file__).resolve().parent.parent

# This project's own corpus, measured 2026-08-05 and recorded in the issue.
# Kept as one fixture so every test that needs "a real reading" uses the same
# one, and so the numbers the boundaries were drafted around stay visible.
CORPUS_2026_08_05 = {
    METRIC_MAIN_THREAD_SHARE_OVER_HALF_WINDOW: 0.389,
    METRIC_CACHE_READS_PER_WRITE: 55.0,
    METRIC_CACHE_WRITE_ONLY_SHARE: 0.007,
    METRIC_MAIN_VS_SUBAGENT_TOKENS_PER_REPLY: 4.0,
}


def synthetic_metric(key, worse_when, cuts, severities):
    """A metric with `cuts` interior boundaries and one severity per range.

    Built the way the real table is built -- adjacent ranges SHARE a boundary
    object -- so a test using it is testing the same shape the module ships.
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
        worse_when=worse_when,
        ranges=tuple(ranges),
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
        (METRIC_CACHE_READS_PER_WRITE, 1.0, "cannot be called either way"),
        (METRIC_CACHE_READS_PER_WRITE, 1.999, "cannot be called either way"),
        (METRIC_CACHE_READS_PER_WRITE, 2.0, "repaid whichever TTL"),
        (METRIC_CACHE_READS_PER_WRITE, 9.999, "repaid whichever TTL"),
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
        got = {
            key: assess(key, value).severity for key, value in CORPUS_2026_08_05.items()
        }
        self.assertEqual(
            got,
            {
                METRIC_MAIN_THREAD_SHARE_OVER_HALF_WINDOW: SEVERITY_ACT,
                METRIC_CACHE_READS_PER_WRITE: SEVERITY_OK,
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
        values = dict(CORPUS_2026_08_05)
        values[METRIC_CACHE_READS_PER_WRITE] = None
        values[METRIC_MAIN_VS_SUBAGENT_TOKENS_PER_REPLY] = None
        result = assess_all(values)
        self.assertEqual(
            result.unmeasured,
            (METRIC_CACHE_READS_PER_WRITE, METRIC_MAIN_VS_SUBAGENT_TOKENS_PER_REPLY),
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
        result = assess_all({key: None for key in METRICS})
        self.assertEqual(result.ranked, ())
        self.assertEqual(result.unmeasured, tuple(sorted(METRICS)))

    def test_a_metric_omitted_from_the_input_is_refused_not_assumed(self):
        # The refusal must be `assess_all`'s own and must say what to pass
        # instead. A bare dict lookup raises `KeyError` too, and a test that
        # accepted that would pass over a function which had stopped checking
        # -- which is exactly what a mutation of the guard showed it doing.
        values = dict(CORPUS_2026_08_05)
        del values[METRIC_CACHE_READS_PER_WRITE]
        with self.assertRaises(KeyError) as caught:
            assess_all(values)
        message = str(caught.exception)
        self.assertIn(METRIC_CACHE_READS_PER_WRITE, message)
        self.assertIn("no value supplied", message)
        self.assertIn("pass None to say", message)

    def test_an_unknown_metric_key_raises_rather_than_returning_none(self):
        # `None` already means "measured nothing". Giving it a second meaning
        # would file a caller's typo under a clean answer.
        with self.assertRaises(KeyError):
            assess("main_thread_share", 0.5)
        with self.assertRaises(KeyError):
            assess_all({**CORPUS_2026_08_05, "invented_metric": 1.0})


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

    def test_exactly_two_boundaries_in_the_whole_table_are_cited(self):
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
            ],
        )

    def test_the_judged_boundaries_are_the_expected_seven(self):
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
        # Two cited boundaries, two DIFFERENT statements: they are different
        # facts about different TTLs, not one fact used twice.
        self.assertEqual(len(by_kind[PROVENANCE_CITED]), 2)
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
        for provenance in (rec.UNREPAID_UNDER_EVERY_TTL, rec.REPAID_UNDER_EVERY_TTL):
            with self.subTest(statement=provenance.statement[:30]):
                self.assertEqual(provenance.checked, "2026-08-04")
                self.assertNotEqual(provenance.checked, RECOMMENDATIONS_AS_OF)

    def test_the_cited_boundaries_state_what_they_do_and_do_not_cover(self):
        for provenance in (rec.UNREPAID_UNDER_EVERY_TTL, rec.REPAID_UNDER_EVERY_TTL):
            with self.subTest(statement=provenance.statement[:30]):
                self.assertIn("5-minute", provenance.covers)
                self.assertIn("1-hour", provenance.covers)
                self.assertIn(rec.CACHE_ARITHMETIC_ENTRY, provenance.covers)
                # The load-bearing exclusion: the multipliers are documented,
                # which TTL any write asked for is not recorded by CPB.
                self.assertIn("does NOT cover", provenance.covers)

    def test_each_cited_boundary_states_the_claim_that_holds_under_both_TTLs(self):
        # The correction this table exists to survive: "one read repays the
        # write" is true of the 5-minute write ONLY, and CPB cannot tell which
        # TTL a write asked for. Each cited boundary must therefore be phrased
        # as a claim about both.
        for provenance in (rec.UNREPAID_UNDER_EVERY_TTL, rec.REPAID_UNDER_EVERY_TTL):
            with self.subTest(statement=provenance.statement[:30]):
                self.assertIn("whichever TTL", provenance.statement)

    def test_the_band_between_the_two_cited_boundaries_refuses_to_call_it(self):
        unresolvable = assess(METRIC_CACHE_READS_PER_WRITE, 1.5).recommendation
        self.assertIn("five-minute", unresolvable)
        self.assertIn("one-hour", unresolvable)
        self.assertIn("does not record which", unresolvable)

    def test_the_measured_one_hour_share_is_recorded_with_its_caveat(self):
        # The evidence that the unresolvable band is worth having. Recorded as
        # a share because the scan was over raw records, not deduped calls.
        self.assertGreater(rec.ONE_HOUR_WRITE_SHARE_AS_MEASURED, 0.0)
        self.assertLess(rec.ONE_HOUR_WRITE_SHARE_AS_MEASURED, 1.0)
        source = " ".join(
            (REPO_ROOT / "recommendations.py").read_text(encoding="utf-8").split()
        )
        self.assertIn("NOT deduped by `message.id`", source)
        self.assertIn("raw records with NO dedupe by", source)

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

    def test_cpb_still_cannot_see_which_TTL_a_write_asked_for(self):
        # The reason the band between the two cited boundaries exists. If
        # ingest ever reads `usage.cache_creation`'s per-TTL split, this goes
        # red and the band can be resolved instead of declared unresolvable.
        ingest_source = (REPO_ROOT / "ingest.py").read_text(encoding="utf-8")
        self.assertNotIn("ephemeral_5m_input_tokens", ingest_source)
        self.assertNotIn("ephemeral_1h_input_tokens", ingest_source)


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
        forward = dict(CORPUS_2026_08_05)
        backward = {k: CORPUS_2026_08_05[k] for k in reversed(list(forward))}
        self.assertNotEqual(list(forward), list(backward))
        self.assertEqual(
            [a.metric for a in assess_all(forward).ranked],
            [a.metric for a in assess_all(backward).ranked],
        )

    def test_todays_corpus_ranks_the_two_firing_metrics_first(self):
        ordered = assess_all(CORPUS_2026_08_05).ranked
        self.assertEqual(
            [a.metric for a in ordered],
            [
                METRIC_MAIN_THREAD_SHARE_OVER_HALF_WINDOW,  # act, depth 0.357
                METRIC_MAIN_VS_SUBAGENT_TOKENS_PER_REPLY,  # act, depth 0.250
                METRIC_CACHE_WRITE_ONLY_SHARE,  # ok,  depth 0.350
                METRIC_CACHE_READS_PER_WRITE,  # ok,  depth 0.182
            ],
        )

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

    def test_an_unbounded_range_reports_no_upper_edge_rather_than_a_number(self):
        result = assess(METRIC_CACHE_READS_PER_WRITE, 55.0)
        self.assertIsNone(result.range_upper)
        self.assertIsNone(result.upper_provenance)

    def test_every_metrics_measurement_names_what_makes_it_unmeasurable(self):
        # Three of the four are ratios with a denominator that can be zero,
        # and a zero denominator is an unmeasured metric, not a zero.
        for key in (
            METRIC_CACHE_READS_PER_WRITE,
            METRIC_CACHE_WRITE_ONLY_SHARE,
            METRIC_MAIN_VS_SUBAGENT_TOKENS_PER_REPLY,
        ):
            with self.subTest(metric=key):
                self.assertIn("unmeasured", METRICS[key].measurement)


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


if __name__ == "__main__":
    unittest.main()
