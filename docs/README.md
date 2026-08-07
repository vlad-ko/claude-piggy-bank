# Documentation index

Longer-form reference that does not belong in the top-level `README.md` (which
is the tool's front door) or in `CLAUDE.md` (which is the working ruleset).

Everything reachable from here is indexed here. A document that is not listed
below is an orphan and should be either linked or removed.

| document | what it is for |
|---|---|
| [Claude API token accounting](claude-api-token-accounting.md) | The API accounting facts CPB's detectors rest on — thinking billed as output, per-model thinking preservation, the cache-miss taxonomy, cache pricing multipliers and minimums, the per-model context window the utilisation figures divide by, task budgets. Each fact carries its source URL and the date it was checked. |
| [Versioning](versioning.md) | What CPB's version number promises: the three surfaces SemVer governs here (the CLI including its exit statuses, the HTTP API, and what a figure *measures*), why `SCHEMA_VERSION` is explicitly excluded and the exact condition that exclusion depends on, which part to bump — with the changes that would have been major worked through — and why the manifest version is a distribution mechanism rather than a label. |
| [The Claude Code plugin](plugin.md) | Why the plugin packaging looks the way it does: which three hooks fire and why `SubagentStop` is the load-bearing one, why every timeout is explicit, where the database lives and when the hook refuses to decide, how failing loudly is reconciled with never interrupting a session, **the marketplace layer** — CPB is its own catalog, and what each field of the single entry turns on — **why `version` is the cache key** an update depends on, the recorded decision on the `CLAUDE.md` validation warning, and **what the plugin may ask the model to do**: the one bounded place a model appears, why the report stays free and offline regardless, and why `/cpb` is never the only path to a number. Each specification claim carries its source URL and the date it was checked; the install and update behaviour is measured here with the dates and the observed paths. |

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
