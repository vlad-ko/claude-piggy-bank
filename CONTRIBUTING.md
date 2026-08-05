# Contributing to Claude Piggy Bank

Thanks for looking. CPB is small on purpose, and a few of its constraints are
load-bearing rather than stylistic — those are worth reading before you open a
PR, because a change that breaks one of them will get pushback even if the code
is good.

## Getting oriented

Four modules, one direction of flow:

```
ingest.py  transcripts -> SQLite
serve.py   SQLite -> JSON        context_window.py is a leaf serve.py reads
index.html JSON -> charts
cpb.py     the entry point over ingest and serve
```

`cpb.py` composes the two scripts rather than replacing them: each keeps its own
`argparse` parser, so a flag has one definition and one help text, and
`python3 ingest.py` keeps working for anyone with it in a script.

```bash
python3 cpb.py ingest        # read this project's own transcripts
python3 cpb.py serve         # http://127.0.0.1:8377/
python3 cpb.py --version
```

Nothing is installed onto your system: `cpb.py` is a file in the checkout that
you run with `python3`.

The repository is also the Claude Code plugin — same code path, no packaging
step. If you are changing anything under `.claude-plugin/`, `hooks/` or
`commands/`, read [`docs/plugin.md`](docs/plugin.md) first: every value in those
JSON files turns on a documented property of the plugin system, and JSON cannot
carry the comment explaining which.

## The four constraints

**1. Standard library only.** Python 3.10+, no pip installs, no Node, no build
step. A tool for understanding cost should not itself require a toolchain to
run. If you need a dependency, open an issue and make the case first.

This one is enforced rather than trusted: the `stdlib-only` CI job parses every
shipped module and fails if any import resolves outside `sys.stdlib_module_names`
or this repo. Before it existed, a single `import requests` would have been
caught only by a reviewer who happened to know the rule.

A second job checks *intent*, which the import scanner cannot see: a **declared**
dependency is someone planning an install step, even before the import lands. It
runs in two deliberate tiers, and it was **narrowed** — it used to reject any
manifest by filename, which failed a `pyproject.toml` containing
`dependencies = []`, a file that declares exactly the thing the rule wants.

| tier | files | rule |
|---|---|---|
| rejected on sight | `requirements*.txt`, `Pipfile`, `poetry.lock`, `uv.lock`, `package.json` and the npm lockfiles, `setup.py`, `setup.cfg` | they exist only to drive an installer, so a dependency-free one is a file with no purpose; the npm manifests additionally imply a Node runtime, which is ruled out outright |
| inspected | `pyproject.toml` | parsed with `tomllib`; passes **iff** it declares no runtime, optional or grouped dependency |

`build-system.requires` is not counted — a build frontend provisions it in an
isolated environment that is discarded, and anything smuggled in that way would
still have to be imported to matter, where the first job catches it.
`dynamic = ["dependencies"]` **fails**: it moves the declaration somewhere the
check cannot read, and a check that cannot see its answer must refuse rather
than report a clean one. That is this repository's central rule applied to its
own CI.

Adding such a file is still a reviewed decision. The narrowing only stopped it
from being an automatic build break — open an issue and make the case first.

**2. Nothing leaves the machine.** No network calls, no telemetry, no CDN
references. This page renders your own prompts, file paths and source code —
a third-party script executing in it is a supply-chain and privacy surface, not
just an availability one. Vendor assets into `vendor/`, as **both** browser
libraries already are — Chart.js for the plot and Alpine.js for the bindings.
`vendor/README.md` records each one's version, origin and SHA-256; the suite
re-checks those digests and that neither bundle contains a way to reach the
network, so a vendored upgrade is a deliberate change to that file rather than a
silent one.

**3. No model produces a figure.** Three claims, three scopes — see
`CLAUDE.md` for the full statement. In short: **every figure** is SQL,
arithmetic or JSON parsing, absolutely and everywhere; **the report** —
`ingest.py`, `serve.py`, `index.html` — runs free and offline, spending no
tokens and making no network call; and **guidance**, in session, may use the
model already present, bounded to `commands/cpb.md`.

The boundary a review will hold you to: **a model may read a finished
measurement and explain it; it may never produce, compute, estimate or fill in
a figure.** A model asked for a number it was not given will supply a fluent,
plausible one — *absence rendered as a value*, arriving by a route no
`Optional[int]` can catch.

Note the cost claim is scoped: the report is free, `/cpb` spends tokens in a
session you are already paying for. Two claims, two scopes; do not restore the
sentence that made them one.

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

## No dollar figures

CPB reports measured tokens and never converts them into money. It used to:
list-rate arithmetic over a hand-maintained table that modelled no subscription
accounting, discount or overage, went stale twice, and diverged from real spend
by more than 2.5x. A precise-looking number wrong by a factor of two is worse
than no number, because the reader cannot see the error — so the estimate was
removed outright rather than qualified, schema column, module and all.

Do not reintroduce a cost estimate, a "relative cost index", or any other
money-shaped field. Panels that used to rank "by spend" rank by **total tokens**,
say so in the heading, and show the model beside each row so the reader weighs
the tiers themselves. A test asserts that no shipped module so much as mentions
the deleted rate table, and that guard is worth keeping total.

The counter-example worth understanding is `context_window.py`, which *is* a
hand-maintained table and is allowed. Its output is a **denominator**
(`context_size / window`, where the window is Anthropic's published hard limit),
so nothing is asserted beyond the division — and a stale window fails **loudly**,
as calls measuring over 100% of it, where a stale rate failed silently in a
plausible dollar figure. That asymmetry is the whole argument; a new table needs
the same one.

## Measurements need provenance

If you add or change a number, say where it came from and when it was checked.
Facts about the Claude API in particular are **model-dependent** and change —
state which models a claim covers rather than presenting it as universal, and
cite the source.

**Two provenance classes, never merged.** *Documented* means cited to an
official source on a date. *Measured here* means counted first-hand, with the
corpus and the scan date. A measurement of what a client writes locally is not a
statement about what the API guarantees, and presenting a judgment in a
documented fact's voice borrows an authority this project has not earned.

That rule is load-bearing, not stylistic. The report's context bands carry two
dates and two provenance strings across the API for exactly this reason: the
window is documented (`WINDOWS_AS_OF`), while where the band boundaries sit is a
product-owner judgment Anthropic publishes nothing about (`BANDS_AS_OF`). They
are free to drift apart and must be able to. Merging them into one date would
claim that re-checking the published window had re-decided where 50% sits.

If you cannot verify something first-hand, say so in the code rather than
asserting it. There is precedent in `transcript_slug()`: the Windows encoding
is documented as a best-known value with its provenance and the symptom to look
for if it is wrong, while the *property* the code actually guarantees is
asserted separately. Two facts with different confidence, two tests.

## Versioning

CPB is **1.0.0** and follows Semantic Versioning. Which part to bump is a rule
with cases and it has one home: [`docs/versioning.md`](docs/versioning.md).
Read it before a change that touches a flag, an exit status, a route, a payload
field, or what a figure measures.

Two clauses catch people out, so they are worth knowing before you open the PR
rather than in review:

- **A stable field name over a changed definition is breaking.** Renaming
  `subagent_tokens` breaks a client loudly; leaving the name and switching the
  statistic underneath it breaks a reader silently, which is worse. Both are
  major here.
- **A `SCHEMA_VERSION` bump is not, on its own, a major bump** — the migration
  machinery, not the version number, is what carries compatibility, and it is
  allowed to absorb the change. That holds *only* while an existing database
  upgrades without data loss and without a refusal. If your schema change
  cannot offer that, you have not written a migration, you have written a major
  release; say so rather than widening `IN_PLACE_UPGRADE_FROM` to make the
  problem go away.

A correction is not a breaking change — a figure that starts reporting what it
always claimed to report is a fix, and belongs on the record in `README.md`
rather than in a major bump. The distinction is drawn in full in that document.
If your change *is* breaking under the rule, say so in the PR body; that is the
cheapest moment for anyone to notice.

## Tests

```bash
python3 -m unittest discover tests -v
```

Most modules also take the dotted form for one class:

```bash
python3 -m unittest tests.test_ingest.StreamedRecordDedupeTest -v
```

**`test_serve` is the exception.** It does `from test_ingest import
build_corpus`, a top-level import that resolves only with `tests/` itself on
`sys.path`, so the dotted form fails with `ModuleNotFoundError: No module named
'test_ingest'` before running anything. Select within it through `discover`:

```bash
python3 -m unittest discover -s tests -p test_serve.py -k ScopeLabellingTest -v
python3 -m unittest discover -s tests -p test_serve.py -k test_<name> -v
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
