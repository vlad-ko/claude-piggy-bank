# Documentation index

Longer-form reference that does not belong in the top-level `README.md` (which
is the tool's front door, and is kept short deliberately) or in `CLAUDE.md`
(which is the working ruleset).

Everything reachable from here is indexed here. A document that is not listed
below is an orphan and should be either linked or removed.

## Getting it running

| document | what it is for |
|---|---|
| [Installing CPB](install.md) | The two ways to run the same code and what each suits: the marketplace plugin, a skills-directory clone you read first, a one-session `--plugin-dir` load, and a plain checkout. Which three hooks the plugin activates and what each ingests; where the plugin keeps your database and the measured evidence that a plugin update does not disturb it; how to upgrade, and how to uninstall without deleting the only copy of your history. |
| [The command line](cli.md) | Every flag, and what each one refuses to do. `cpb.py` and the two scripts it composes; ingesting a directory against ingesting exactly one transcript, with the measured cost of each; and the `--db` / `CPB_DB` / default resolution order, including why the default is plugin-aware and where it refuses rather than falling back. |

## Reading the numbers

| document | what it is for |
|---|---|
| [What each figure means](metrics.md) | The headline median and why it displaced the mean; the context-window bands and why only the top two carry a verdict; the two provenances the page keeps apart, per boundary rather than per table; what the bands refuse to say (unknown model, unmeasured call, over-100% window); how the data-age pair reads; what a total covers when one database holds several projects; the sample floors; and why there are no dollar figures. |
| [Where the data comes from](data-sources.md) | The two transcript globs CPB treats as data and the third path it treats as an index only; what one API call is; the three ways a cache write is stored. Why the format is Anthropic-internal and documented to change, what CPB does about that (`source_shape`), and the one test that reads a real transcript. Ends with the retention window: past it, your database is the only copy of your history. |
| [Claude API token accounting](claude-api-token-accounting.md) | The API accounting facts CPB's detectors rest on — thinking billed as output, per-model thinking preservation, the cache-miss taxonomy, cache pricing multipliers and minimums, the per-model context window the utilisation figures divide by, task budgets. Each fact carries its source URL and the date it was checked. |

## How it is packaged

| document | what it is for |
|---|---|
| [The Claude Code plugin](plugin.md) | Why the plugin packaging looks the way it does: which three hooks fire and why `SubagentStop` is the load-bearing one, why every timeout is explicit, where the database lives and when the hook refuses to decide, how failing loudly is reconciled with never interrupting a session, **the marketplace layer** — CPB is its own catalog, and what each field of the single entry turns on — **why `version` is the cache key** an update depends on, the recorded decision on the `CLAUDE.md` validation warning, and **what the plugin may ask the model to do**: the one bounded place a model appears, why the report stays free and offline regardless, and why the skill is never the only path to a number, **why the command is namespaced** by the plugin and what that means for a skills-directory install. Each specification claim carries its source URL and the date it was checked; the install and update behaviour is measured here with the dates and the observed paths. |

## What changed, and what was wrong

Two records, because a wrong number and a broken script send a reader to
different places. Neither is a changelog of commits, and each links the other.

| document | what it is for |
|---|---|
| [Release record](releases.md) | What each release changed **for a user** and what they have to do about it — where a break classified by `versioning.md` is actually announced, since the version number alone cannot say what broke. Entries state the change in the terms a caller meets it in, and a release with nothing to migrate says so. `tests/test_release_notes.py` pins the shipped `cpb.VERSION` to having an entry, so the record cannot fall a release behind. |
| [The record of corrections](corrections.md) | Figures that were wrong and are now right, in full: what the number was, what it is, and how the difference was measured — starting with the dedupe defect that inflated call counts by 2.36x on the main thread and 1.91x for subagents, and by *different* factors, which distorted the comparison in shape. A correction is deliberately not a breaking change; breaks are announced in `releases.md` instead. |
| [Versioning](versioning.md) | What CPB's version number promises: the three surfaces SemVer governs here (the CLI including its exit statuses, the HTTP API, and what a figure *measures*), why `SCHEMA_VERSION` is explicitly excluded and the exact condition that exclusion depends on, which part to bump — with the changes that would have been major worked through — and why the manifest version is a distribution mechanism rather than a label. |

## What a document here has to carry

Same standard as the code, for the same reason: this project is about
measurement, and an unsourced claim in a reference doc becomes a wrong number
in a detector.

- **Provenance per claim.** Where it came from and when it was checked. Not
  once for the file — per claim, because they age at different rates.
- **Model-dependence stated.** Facts about the Claude API are per model. Name
  the models a claim covers rather than writing "Claude".
- **Two provenance classes, never merged.** *Documented* (cited to an official
  source on a date) and *measured here* (counted first-hand, with the corpus
  and the scan date). A measurement of what a client writes locally is not a
  statement about what the API guarantees. A third class is legitimate and must
  be labelled as loudly: *product-owner judgment*, dated, with no source
  because there is none.
- **Provenance per boundary, not per table.** Where several judgments are
  presented together — rows of a table, thresholds of a detector — each carries
  its own. A file-level or table-level provenance line lets a judged value
  inherit a cited value's credibility by sitting next to it, which is the
  `band_provenance` failure one level down. See the `context` block discussion
  in `CLAUDE.md`.
- **Unverified is a legitimate state.** A claim that could not be confirmed is
  marked unverified with what was checked, not quietly asserted or quietly
  dropped.
- **A version literal is a decaying fact.** `cpb.VERSION` is the authority and
  `python3 cpb.py --version` is how a reader gets it. Where a document states
  the shipped version anyway, it wraps it in the `cpb:version` marks so
  `DocsStateTheShippedVersionTest` pins it; the top-level `README.md`
  deliberately states no version at all.
