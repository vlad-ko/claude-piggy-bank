# Claude API token accounting — checked facts

Reference for the accounting rules CPB's detectors depend on. Each fact says
which models it covers, cites the source it was checked against, and carries
the date it was checked.

This file exists because these facts are counterintuitive, individually easy to
get wrong, and expensive to re-derive — and because three of them had already
produced a wrong conclusion in this project before anyone checked:

- "thinking is unmeasurable" — wrong, there is a direct field ([TA-2](#ta-2)).
- "thinking costs nothing once the turn is over" — wrong, on most current
  models it is re-billed as input on every later turn ([TA-4](#ta-4)).
- "a residual is the only route to thinking tokens" — the field exists; Claude
  Code does not persist it ([TA-2](#ta-2)).

Until now that knowledge lived in a conversation transcript, which is precisely
the failure mode this tool measures.

## How to read an entry

Every fact carries one of two provenance classes, and they are never merged:

- **Documented** — stated by Anthropic's official documentation at the cited
  URL, fetched and read on the **checked** date. A documented fact is only as
  current as that date; the docs change.
- **Measured here** — counted first-hand over the local transcript corpus
  described below. A measurement is about *what Claude Code writes on this
  machine at this version*, never about what the API guarantees.

Where the source hedges, this file hedges. Where a fact is model-dependent, the
models are named; nothing here is a universal claim about "Claude" unless the
source makes one.

To re-check any entry, append `.md` to its documentation URL to get the
markdown source of that page (`…/build-with-claude/thinking.md`). Links written
as `docs.claude.com/en/docs/…` redirect to `platform.claude.com/docs/en/…`; the
latter form is cited here because it is what resolved on the checked date.

<a id="corpus"></a>

## The corpus behind every "measured here" figure

Scanned **2026-08-05T03:22Z** (2026-08-04 local, the checked date throughout
this file) over `~/.claude/projects` on the author's machine:

| | |
|---|---|
| transcript files (`*.jsonl`) | 2,977 |
| assistant records carrying `message.usage` | 336,199 |
| distinct `message.id` (i.e. real API calls) | 166,115 |
| record timestamps | 2026-06-06 → 2026-08-05 |
| Claude Code versions | 2.1.161 – 2.1.222 |

Models, by record count: `claude-opus-4-8` 246,324, `claude-opus-5` 69,063,
`claude-sonnet-5` 8,917, `claude-fable-5` 7,217, `claude-sonnet-4-6` 3,267,
`claude-haiku-4-5-20251001` 1,303, `<synthetic>` 108 (Claude Code's local
error placeholders, which are not API responses).

Record counts are pre-dedupe: Claude Code writes one record per streamed
content block and each repeats the same `message.usage`, so a per-call figure
must be counted over distinct `message.id` (see the dedupe note in
`CLAUDE.md`). Where a figure below is per-call it says so.

This is one machine and one user. It is a sample of what Claude Code writes,
not a population, and a different version may write something else.

---

<a id="ta-1"></a>

## TA-1 — Thinking tokens are billed as output tokens

**Applies to:** every model that supports thinking.
**Provenance:** Documented. **Checked:** 2026-08-04.
**Source:** <https://platform.claude.com/docs/en/build-with-claude/thinking>

> Thinking has a cost: the tokens Claude spends reasoning are billed as output
> tokens, even when the thinking text isn't returned to you, and they count
> toward `max_tokens` alongside the response text.

Thinking is therefore already inside `usage.output_tokens` — it is not a
missing token class, it is an unlabelled share of a class CPB already stores.
The billing consequence of a *later* turn re-reading that thinking is a
different fact, and is model-dependent: see [TA-4](#ta-4).

<a id="ta-2"></a>

## TA-2 — The API reports thinking tokens directly; Claude Code does not persist the field

**Applies to:** the Messages API generally (field), Claude Code 2.1.161–2.1.222
(the non-persistence).
**Provenance:** Documented (field) + measured here (absence).
**Checked:** 2026-08-04.
**Sources:**
<https://platform.claude.com/docs/en/build-with-claude/thinking-steering-and-cost#pricing>,
<https://platform.claude.com/docs/en/api/messages>

> To see how many billed output tokens were spent on internal reasoning, read
> `usage.output_tokens_details.thinking_tokens` in the response. This value
> reflects the raw reasoning the model generated (not the summarized text
> returned in the body) and is always less than or equal to `output_tokens`.
> Subtract it from `output_tokens` to approximate the non-reasoning portion of
> the output. **When streaming, this breakdown appears only on the final
> `message_delta` event.**

The docs are explicit that `output_tokens` stays the authoritative billing
total and `output_tokens_details` is "a read-only breakdown for observability".
So the field answers "how much of this call was thinking", not "what am I
charged".

**Measured here:** of **336,199** assistant records carrying `message.usage`,
**0** carry `output_tokens_details`. The `usage` keys that do appear
corpus-wide are:

```text
input_tokens, cache_creation_input_tokens, cache_read_input_tokens,
output_tokens, service_tier, cache_creation (ephemeral_5m_input_tokens,
ephemeral_1h_input_tokens), inference_geo, server_tool_use, iterations, speed
```

Claude Code streams, and the breakdown that arrives only on the final
`message_delta` is not carried into the persisted record. That is a gap in what
the client writes, not an API limitation — the distinction matters, because it
means the fix is upstream and the local workaround (a residual estimate) must
be labelled an estimate. Issue [#3](https://github.com/vlad-ko/claude-piggy-bank/issues/3)
tracks both tracks; it measured 0 of 84,994 records on a single project, and
this wider scan (all projects on the machine) agrees at 0 of 336,199.

<a id="ta-3"></a>

## TA-3 — Billed output tokens do not equal visible tokens, under either `display` setting

**Applies to:** every model that supports thinking; `display` defaults are
per model (below).
**Provenance:** Documented. **Checked:** 2026-08-04.
**Sources:**
<https://platform.claude.com/docs/en/build-with-claude/thinking-steering-and-cost#pricing>,
<https://platform.claude.com/docs/en/build-with-claude/thinking#controlling-thinking-display>

|                             | `display: "summarized"`                | `display: "omitted"`                    |
|---|---|---|
| Output tokens (billed)      | full thinking Claude generated         | same as summarized                      |
| Output tokens (visible)     | the summarized thinking text           | zero (the `thinking` field is empty)    |

> The billed output token count does **not** match the visible token count in
> the response. You are billed for the full thinking process, not the thinking
> content visible in the response.

Two further points from the same pages, both load-bearing for any
content-derived estimate:

- With `display: "summarized"` the visible text is a **summary produced by a
  different model**; you are charged for the original thinking, not the
  summary, and the summary is not itself charged.
- `display: "omitted"` is the **default** on Claude Fable 5, Claude Mythos 5,
  Claude Opus 5, Claude Sonnet 5, Claude Opus 4.8, Claude Opus 4.7 and Claude
  Mythos Preview. `"summarized"` is the default on Claude Opus 4.6, Claude
  Sonnet 4.6 and earlier. Omitting reduces latency, not cost.

Consequence for CPB: **no character- or content-based measure can see thinking
volume.** CPB already encodes half of this — `ContentBlock.chars` is
`Optional[int]` precisely because thinking blocks persist with empty text — and
the other half is the rule that a thinking share derived from visible content
is not a measurement of thinking at all.

<a id="ta-4"></a>

## TA-4 — Thinking-block preservation is per model; on keep-all models thinking is re-billed as input every later turn

**Applies to:** as listed — this is the most model-dependent fact in this file
and must never be stated universally.
**Provenance:** Documented. **Checked:** 2026-08-04.
**Source:**
<https://platform.claude.com/docs/en/build-with-claude/thinking#thinking-block-preservation-by-model>

> **Keep all prior turns:** Claude Opus 4.5 and later Opus models, Claude
> Sonnet 4.6 and later Sonnet models, Claude Fable 5, Claude Mythos 5, and
> Claude Mythos Preview.
>
> **Keep the last turn only:** earlier Opus and Sonnet models, and all Haiku
> models through Claude Haiku 4.5. When you pass older thinking blocks back,
> the API strips them automatically.

And on what that costs
(<https://platform.claude.com/docs/en/build-with-claude/thinking#thinking-and-the-context-window>):

> **Prior-turn thinking** depends on the preservation default. On models that
> keep all prior turns, previous thinking blocks remain in context, count
> toward the window, and are **billed as input tokens** like the rest of the
> conversation history. On models that keep only the last turn, the API strips
> older thinking blocks automatically when you pass them back, so they don't
> consume window space or input tokens.

So on a keep-all model thinking is billed **twice, and the second charge
recurs**: once as output when generated ([TA-1](#ta-1)), then as input on every
subsequent request in the conversation.

Three qualifications the source makes, all easy to lose in summary:

1. The keep-last-turn list is written as "all Haiku models **through** Claude
   Haiku 4.5" — it is bounded at a named model, not a statement about Haiku
   forever. Key detectors off the model id, not off a family name.
2. During a tool-use loop, thinking blocks are cached alongside tool results
   and "count as input tokens in your usage metrics when read from the cache"
   — that is true in both regimes, for the duration of the assistant turn.
3. **Switching models mid-conversation:** thinking blocks are tied to the model
   that produced them. Other models "silently ignore them rather than rejecting
   the request, but ignored blocks still add input tokens." CPB's own corpus
   spans six model ids, so a session that switches models is not hypothetical.

**Measured here:** of the six real model ids in the corpus, five
(`claude-opus-4-8`, `claude-opus-5`, `claude-sonnet-5`, `claude-fable-5`,
`claude-sonnet-4-6`) are in the keep-all set and one
(`claude-haiku-4-5-20251001`, 1,303 records) is in the keep-last-turn set. A
detector that assumes keep-all everywhere would be right on 99.6% of records
here and wrong on the rest — which is exactly the shape of error that survives
review.

<a id="ta-5"></a>

## TA-5 — A specialized system prompt is added automatically when thinking is active

**Applies to:** stated without model qualification on the cited page.
**Provenance:** Documented. **Checked:** 2026-08-04.
**Source:**
<https://platform.claude.com/docs/en/build-with-claude/thinking-steering-and-cost#pricing>

> When thinking is active, a specialized system prompt is automatically
> included to support this feature.

The docs do not publish its token count, and it is not separable in the `usage`
fields. Treat it as a known, unquantified addition to the resident baseline:
report that it exists rather than a number for it. Claude Code transcripts
carry no `system` field at all (see [TA-7](#ta-7) — measured), so CPB cannot
size it locally either.

<a id="ta-6"></a>

## TA-6 — Thinking config and `effort` are rendered into the prompt, so changing either invalidates the cache

**Applies to:** all models for the message-level effect; the tools/system-level
effect is explicitly model-specific.
**Provenance:** Documented. **Checked:** 2026-08-04.
**Sources:**
<https://platform.claude.com/docs/en/build-with-claude/prompt-caching#what-invalidates-the-cache>,
<https://platform.claude.com/docs/en/build-with-claude/thinking#thinking-and-prompt-caching>,
<https://platform.claude.com/docs/en/build-with-claude/effort>

> Changing the `output_config.effort` value always invalidates message blocks,
> with the same model-specific effect on tool and system caches as thinking
> parameters. **Setting effort explicitly to the model's default is equivalent
> to omitting it and does not invalidate.**

The mechanism is the reason it is not obvious: the thinking configuration and
the resolved effort level are *rendered into the prompt itself*, so a change
starts a new cache prefix. The same page marks the tools-cache and
system-cache columns "Model-specific" for both thinking parameters and effort:
message blocks always miss, tool and system blocks miss only "on models that
render the configuration ahead of them". The docs do not enumerate which
models those are, so **which caches are lost is unverified per model** — do not
guess it.

The API default is `high` effort
(<https://platform.claude.com/docs/en/build-with-claude/effort>), which is why
"set it to the default" and "omit it" cost the same.

**Relevant to CPB:** the transcript carries an `effort` field on records
(present on 79,750 records in the [corpus](#corpus) scan), so an effort change
mid-session is directly observable locally even though the API classes it under
`unavailable` rather than a named cache-miss reason ([TA-7](#ta-7)).

<a id="ta-7"></a>

## TA-7 — The cache-miss taxonomy has six members, not four — and Claude Code persists it

**Applies to:** the Claude API only (not Amazon Bedrock, not Google Cloud);
beta as of the checked date.
**Provenance:** Documented (taxonomy) + measured here (persistence).
**Checked:** 2026-08-04.
**Source:**
<https://platform.claude.com/docs/en/build-with-claude/cache-diagnostics>

Cache diagnostics compares a request against the previous one and reports the
**earliest** divergence point. It is opt-in: beta header
`cache-diagnosis-2026-04-07`, plus `diagnostics.previous_message_id` on the
request. `cache_miss_reason` is a discriminated union on `type`:

| `type` | meaning |
|---|---|
| `model_changed` | the `model` differs; the cache is per-model |
| `system_changed` | the `system` parameter differs (typically an interpolated timestamp or id) |
| `tools_changed` | tools added, removed, reordered, or non-deterministically serialized |
| `messages_changed` | an earlier `messages` entry was altered, reordered or removed rather than appended to |
| `previous_message_not_found` | no stored fingerprint for that id — **not** evidence that the request changed |
| `unavailable` | no comparison was produced. Includes the case where model, system and tools match but another prompt-affecting parameter differs (`tool_choice`, `thinking`, `context_management`, `output_config`, `output_format`, or the active beta headers), and conversations longer than the comparison horizon |

Two qualifications:

- The four `*_changed` types carry `cache_missed_input_tokens`, which the docs
  describe as "derived from byte lengths before tokenization, so treat it as a
  magnitude indicator rather than a billing number. It can differ from (and
  occasionally exceed) `usage.input_tokens`." That is Anthropic labelling its
  own figure an estimate; CPB must carry the label through.
- Only the earliest divergence is reported. Later ones may be hidden behind it,
  so the absence of a `tools_changed` does not prove the tools were stable.

**Measured here — the significant finding.** Claude Code sends the beta header,
and **the transcripts persist the result**: `message.diagnostics` is present on
every real API-response record in the [corpus](#corpus) (336,091 of 336,199
records; the 108 without it are the `<synthetic>` local error placeholders).
It is `null` on 333,347 records — meaning "first turn, or no divergence found"
— and carries a `cache_miss_reason` object on the rest. By **distinct
`message.id`**, i.e. per API call:

| `cache_miss_reason.type` | calls |
|---|---|
| `unavailable` | 530 |
| `messages_changed` | 321 |
| `previous_message_not_found` | 203 |
| `tools_changed` | 94 |
| `system_changed` | 36 |
| `model_changed` | 10 |

All six types occur, including the two — `system_changed` and `tools_changed`
— that a transcript-only analysis was assumed to be unable to separate, and
each observed reason carried a `cache_missed_input_tokens` magnitude.

This changes the premise of issue
[#5](https://github.com/vlad-ko/claude-piggy-bank/issues/5), which was written
on the assumption that the taxonomy is available to the caller at request time
only and must be re-implemented offline from message history. On these Claude
Code versions it does not have to be inferred; it can be **read**. What still
has to be inferred is anything the reason does not name, and the transcript
remains missing the inputs a re-implementation would need: **measured, 0 of
336,199 records carry a `system` or `tools` field.** Also note that the
reason is attached to the *call*, and CPB stores one row per `message.id`, so
the classification lands naturally on `api_calls` — but it is Claude Code
behaviour, not an API guarantee, so a detector must treat its absence as
INCONCLUSIVE rather than as "no divergence".

<a id="ta-8"></a>

## TA-8 — Cache writes carry a 25% markup; the cacheable minimum is per model and not monotonic

**Applies to:** the multipliers are uniform across the published price table;
the minimum length is explicitly per model.
**Provenance:** Documented. **Checked:** 2026-08-04.
**Source:**
<https://platform.claude.com/docs/en/build-with-claude/prompt-caching#pricing>,
<https://platform.claude.com/docs/en/build-with-claude/prompt-caching#cache-limitations>

Multipliers on the base input price, stated by the docs and consistent across
every model in their price table:

| | multiplier of base input |
|---|---|
| 5-minute cache write | **1.25x** |
| 1-hour cache write | **2x** |
| cache read / refresh | **0.1x** |

**When the markup repays.** Sending the same prefix *n* times costs `n x 1.0`
uncached, against `1.25 + 0.1 x (n-1)` cached. At n=2 that is **1.35 against
2.00** — so a *single* cache read more than repays the 5-minute write markup:
it costs 0.25 extra and saves 0.90. Break-even is the first read, i.e. the
second time the prefix is sent. The 1-hour write is the one that needs **two**
reads: `2.0 + 0.1 x (n-1)` is 2.10 against 2.00 at n=2, still a loss, and 2.20
against 3.00 at n=3, a win. Stating either as "repays on the second hit"
understates 5-minute caching by a hit; keep the arithmetic, not the slogan.

**Minimum cacheable prompt length** — below this, `cache_control` is silently
ignored and no error is returned:

| model | minimum tokens |
|---|---|
| Claude Opus 5, Claude Fable 5, Claude Mythos 5 | 512 |
| Claude Mythos Preview, Claude Opus 4.7 | 2,048 |
| Claude Opus 4.8, Claude Sonnet 5, Claude Sonnet 4.6, Claude Sonnet 4.5, Claude Opus 4.1, Claude Opus 4, Claude Sonnet 4 | 1,024 |
| Claude Opus 4.6, Claude Opus 4.5 | 4,096 |
| Claude Haiku 4.5 | 4,096 |
| Claude Haiku 3.5 | 2,048 |

Not monotonic across generations: Haiku went **up**, 2,048 (Haiku 3.5) to 4,096
(Haiku 4.5), while Opus came down 4,096 → 2,048 → 1,024 → 512 across 4.5/4.6 →
4.7 → 4.8 → 5. A single "minimum cacheable prompt" constant is therefore wrong
by construction; it is a per-model lookup.
These figures are for the Claude API, Claude Platform on AWS, Google Cloud and
Microsoft Foundry; Bedrock is documented separately by AWS.

The corresponding usage fields are `cache_creation_input_tokens` (written),
`cache_read_input_tokens` (read) and `input_tokens` (only the tokens *after*
the last breakpoint), so `total_input = read + creation + input_tokens`. If
both cache fields are `0`, the prompt was not cached at all — possibly because
it fell under the minimum above.

<a id="ta-9"></a>

## TA-9 — Task budgets: unsupported on Claude Code; the accounting model is still worth borrowing

**Applies to:** beta on Claude Opus 5, Claude Fable 5, Claude Mythos 5, Claude
Opus 4.8, Claude Opus 4.7 (header `task-budgets-2026-03-13`). Not supported on
Claude Sonnet 5, Claude Opus 4.6, Claude Sonnet 4.6, Claude Haiku 4.5.
**Provenance:** Documented. **Checked:** 2026-08-04.
**Source:** <https://platform.claude.com/docs/en/build-with-claude/task-budgets>

> Task budgets are not supported on Claude Code or Cowork surfaces.

**Do not build a task-budget detector.** There is nothing to measure: the
countdown is injected server-side and visible only to the model — "API
responses do not include a remaining-budget field: there is no `task_budget`
information in the response `usage` object". Even on a supported surface it
would be unobservable from a transcript.

The accounting model is the part worth taking. The budget counts **what the
model sees this turn**, not what the client resends:

| turn | request payload sent | counted against budget |
|---|---|---|
| 1 | ~20 | 5,000 |
| 2 | ~7,800 | 6,800 |
| 3 | ~13,000 | 7,200 |
| **total** | **~20,820 sent across requests** | **19,000 counted** |

> Your client sent the turn-1 user message three times and the turn-1 assistant
> message twice, but each was counted once.

Two distinct quantities that a naive sum conflates: **transmitted** volume
(what CPB's per-call token classes measure, and what is billed) and **new
content** volume (what the conversation actually added). A CPB figure that sums
per-call input tokens across a session is measuring the first — correctly, for
cost — but it is not a measure of how much the conversation grew, and labelling
it as such would be the "aggregate that does not name its set" defect.

---

## Corrections made while checking

Recorded because the list this file was written from is cited elsewhere, and a
fact that was quietly fixed here would go on being wrong there.

1. **"Preservation: keep-all on Opus 4.5+/Sonnet 4.6+, stripped on earlier
   models and all Haiku."** Incomplete in both directions. The keep-all set
   also names Claude Fable 5, Claude Mythos 5 and Claude Mythos Preview
   (relevant: `claude-fable-5` is in this corpus), and the strip set is written
   as "all Haiku models **through** Claude Haiku 4.5", not all Haiku
   unconditionally. See [TA-4](#ta-4).
2. **"The cache-miss taxonomy is `model_changed` / `system_changed` /
   `tools_changed` / `messages_changed`."** There are six types; the other two
   (`previous_message_not_found`, `unavailable`) are the ones that mean *no
   comparison was produced*, and conflating either with a real divergence would
   invent a cause. `unavailable` is also where thinking/effort/`output_config`
   changes land. See [TA-7](#ta-7).
3. **"Transcripts cannot separate the last three."** Wrong on this machine.
   Claude Code persists `message.diagnostics.cache_miss_reason`, and all six
   types — `system_changed` and `tools_changed` included — occur in the corpus.
   The underlying inputs (`system`, `tools`) are indeed absent from transcripts
   (measured: 0 of 336,199 records), so a *re-derivation* could not separate
   them; reading Anthropic's own classification can. See [TA-7](#ta-7).
4. **"Cache writes carry a 25% markup, repaid on the second hit."** The markup
   is repaid by the **first** read — the second time the prefix is sent. The
   1-hour write (2x) is the one that needs two reads. See [TA-8](#ta-8).
5. **Task budgets** are additionally beta-gated per model, not merely
   unsupported on Claude Code; and the response carries no budget field at all,
   which is a stronger reason not to build the detector than surface support.
   See [TA-9](#ta-9).

Nothing in the received list turned out to be unverifiable. Every entry above
resolved to an official source or to a first-hand count; where a sub-claim did
not (which caches an effort change invalidates, per model), it is marked
unverified in place rather than smoothed over.

## Where these facts are used

Forward links, so a change here is traceable to what it affects:

- [#3 — Claude Code drops `usage.output_tokens_details.thinking_tokens`](https://github.com/vlad-ko/claude-piggy-bank/issues/3) — [TA-1](#ta-1), [TA-2](#ta-2), [TA-3](#ta-3)
- [#5 — offline cache-miss diagnostics](https://github.com/vlad-ko/claude-piggy-bank/issues/5) — [TA-6](#ta-6), [TA-7](#ta-7), [TA-8](#ta-8)
- [#6 — thinking re-billed as input on every later turn](https://github.com/vlad-ko/claude-piggy-bank/issues/6) — [TA-1](#ta-1), [TA-3](#ta-3), [TA-4](#ta-4), [TA-5](#ta-5), [TA-6](#ta-6)
- [#11 — insight layer, detectors for the measured lessons](https://github.com/vlad-ko/claude-piggy-bank/issues/11) — all of them; [TA-9](#ta-9) is the one that says *don't* build a detector
- [#16 — cache economics observable](https://github.com/vlad-ko/claude-piggy-bank/issues/16) — [TA-7](#ta-7), [TA-8](#ta-8)

## Maintaining this file

- A fact whose source you cannot reach is **not** re-stated on faith. Mark it
  unverified, with what you checked and when, and say what would settle it.
- Re-checking a fact means updating its **checked** date even when nothing
  changed — an old date is information.
- If a documented fact changes, keep the previous statement and date it. A
  detector built against the old behaviour needs to know when the ground moved.
- A "measured here" figure is re-measured, not adjusted. Its corpus line
  (files, records, versions, dates) is part of the figure.
