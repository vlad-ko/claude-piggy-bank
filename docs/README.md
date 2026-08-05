# Documentation index

Longer-form reference that does not belong in the top-level `README.md` (which
is the tool's front door) or in `CLAUDE.md` (which is the working ruleset).

Everything reachable from here is indexed here. A document that is not listed
below is an orphan and should be either linked or removed.

| document | what it is for |
|---|---|
| [Claude API token accounting](claude-api-token-accounting.md) | The API accounting facts CPB's detectors rest on — thinking billed as output, per-model thinking preservation, the cache-miss taxonomy, cache pricing multipliers and minimums, task budgets. Each fact carries its source URL and the date it was checked. |

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
  statement about what the API guarantees.
- **Unverified is a legitimate state.** A claim that could not be confirmed is
  marked unverified with what was checked, not quietly asserted or quietly
  dropped.
