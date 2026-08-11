# The record: corrections to figures that were wrong

There is no changelog. This is one of two records that stand in for one, and it
is the one about **numbers**: a figure that was wrong and is now right, kept in
full, with what it was, what it is, and how the difference was measured. A tool
about measurement should show its own corrections rather than quietly restate
them, and a number's history is more useful beside the number than as a one-line
entry in a list.

A correction is **not** a breaking change — the figure always claimed to count
one thing and had been counting another — which is a distinction
[`versioning.md`](versioning.md) draws deliberately.

**Breaks live elsewhere.** A change to behaviour a caller's script depended on
is announced per release in [`releases.md`](releases.md), because someone whose
script stopped working is not reading a record of corrected figures. The two
records stay separate and each links the other.

## The defect that made the headline numbers wrong

`api_calls` used to count transcript *records*. Claude Code writes one record
per streamed content block, and each repeats the same `message.usage` object,
so a single API response was counted many times. On one real corpus:

| | counted before | actual | inflation |
|---|---|---|---|
| main-thread calls | 85,324 | 36,167 | 2.36x |
| subagent calls | 242,242 | 126,757 | 1.91x |

The factor differed by scope — **2.36x against 1.91x** — so any main-thread
versus subagent comparison was distorted in **shape**, not merely in
magnitude: the two sides were scaled by different amounts before being read
against each other. A conclusion, not just a scale, was wrong.

Ingest now emits one row per distinct `message.id`, and the surviving row is
the record with the **greatest `output_tokens`**, ties resolving to the later
record. Taking the first would have under-counted output by roughly 99%.

That rule is often described as "the last record wins", and on the corpus above
the two were the same record every time: `output_tokens` was non-decreasing
over the records of one id in **4,928 of 4,928** cases, so the greatest record
*was* the final one. That is a measurement, not a guarantee, and it has since
drifted. Re-measured **2026-08-05** across 49 main-thread transcripts on one
machine, non-decreasing holds for **26,998 of 27,106** multi-record ids
(**99.6%**). On the **108** ids where output falls, keeping the last record
would report a *smaller* finished total than a record already seen — 107,810
output tokens understated in aggregate, up to 6,858 on a single id. `max` is
kept precisely because it does not depend on the tendency holding. Both
percentages are dated samples from a corpus that keeps growing, not constants:
the denominator moved from 27,106 to 27,110 within ten minutes of that scan,
because the session doing the measuring was being transcribed into it. Expect a
different denominator, and re-measure before quoting either figure.

Two residual cases are counted and printed rather than hidden. On the earlier
corpus, **109 ids** whose records disagreed on something other than
`output_tokens` keep one whole real record, never a per-field maximum -- that
would report a call which both wrote and did not write cache, a combination
present in no real response. That count ranges over a **different set** from
the 108 above (disagreeing beyond `output_tokens`, versus `output_tokens`
falling) on a different corpus and date; on the 2026-08-05 corpus those two
sets happened to be the same 108 ids, which is an observation about that corpus
rather than a property of either rule. Records with **no** `message.id` each
stay their own call; on this corpus there were none, but dropping them would
delete real spend and grouping them would merge unrelated calls.

Publishing a tool whose documentation recorded its own broken numbers was a
choice. The alternative was to fix it privately first, and the point of this
project is that measurement should be inspectable, including when it is wrong.
