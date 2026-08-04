# Contributing to Claude Piggy Bank

Thanks for looking. CPB is small on purpose, and a few of its constraints are
load-bearing rather than stylistic — those are worth reading before you open a
PR, because a change that breaks one of them will get pushback even if the code
is good.

## The four constraints

**1. Standard library only.** Python 3, no pip installs, no Node, no build
step. A tool for understanding cost should not itself require a toolchain to
run. If you need a dependency, open an issue and make the case first.

**2. Nothing leaves the machine.** No network calls, no telemetry, no CDN
references. This page renders your own prompts, file paths and source code —
a third-party script executing in it is a supply-chain and privacy surface, not
just an availability one. Vendor assets into `vendor/`, as the chart library
already is.

**3. No model in the loop.** Every detector is SQL, arithmetic, or JSON
parsing. Adding an API call would break both the offline guarantee and the
premise that measuring cost is free.

**4. Do not overwhelm the UI.** The information is useful; a wall of it is not.
A new detector earns its space by displacing or annotating something, not by
appending another table. Prefer a small number of surfaces that answer a
question.

## Absence is never a value

This is the rule most likely to come up in review.

A read that cannot produce a trustworthy answer must **refuse** — return
`None`, report INCONCLUSIVE, or raise — rather than return a plausible number
someone will act on. Concretely:

- Never default a missing measurement to `0`. A genuine `0` is a healthy sample
  and must stay distinguishable from *no sample*. Count samples separately from
  the aggregate.
- Never let a `catch` return a benign default. A swallowed failure that returns
  an empty result hands the caller a fabricated answer.
- An aggregate must name the set it ranges over. A main-thread-only figure that
  reads like a session total is a wrong number even when every input was right.

There is a worked example in the code: thinking blocks are persisted by Claude
Code with empty text, so `ContentBlock.chars` is `Optional[int]` and a stripped
block records `None`. Recording `0` would make a composition table report
"thinking: 0.0%" — stating that thinking costs nothing, which is false, and
which no reader would question because it looks like a measurement.

## Measurements need provenance

If you add or change a number, say where it came from and when it was checked.
Facts about the Claude API in particular are **model-dependent** and change —
state which models a claim covers rather than presenting it as universal, and
cite the source.

If you cannot verify something first-hand, say so in the code rather than
asserting it. There is precedent in `transcript_slug()`: the Windows encoding
is documented as a best-known value with its provenance and the symptom to look
for if it is wrong, while the *property* the code actually guarantees is
asserted separately. Two facts with different confidence, two tests.

## Tests

```bash
python3 -m unittest discover tests -v
```

- Assert the **state change**, not that something succeeded.
- A fixture must not make the defect undetectable: when two quantities could
  diverge, pin them deliberately unequal, or either source passes and the test
  has no opinion.
- Verify a test has teeth by mutation — change one line of the implementation,
  confirm the test goes red, then change it back. A test that stays green under
  a deliberate mutation is not a test.
- Fixtures are synthetic. Never commit captured session content: real
  transcripts contain prompts, file paths and source code.

## Reporting a bug

Most valuable are cases where CPB reports a number that is *wrong* rather than
missing — those are the failures the design is meant to prevent. Include the
figure you saw, what you expected, and how you know.
