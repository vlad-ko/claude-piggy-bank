"""API list-rate pricing table for the usage-report tool (#4948).

**These are PUBLISHED LIST RATES, not a bill.** `PRICES` below is a
hand-maintained copy of Anthropic's public per-MTok pricing page. It requires
manual maintenance -- nobody re-fetches it automatically -- so it WILL go
stale the moment Anthropic changes a rate and nobody updates this file. Any
dollar figure this module produces (via `cost_usd`) is therefore a **list-rate
ESTIMATE**: arithmetic over measured token counts, computed against whatever
this table currently says. It reflects NEITHER subscription-plan accounting
(Pro/Max included usage) NOR discounts NOR overage behavior, and it is not
adjusted for how any given account is actually billed. See the "Cost figures
are an ESTIMATE" section of `README.md` for a worked example of just how far
an estimate like this can diverge from an actual bill.

ONE config dict: per-MTok list rates keyed by model-id PREFIX. Lookup is by
LONGEST matching prefix, with a boundary character (`-`) or exact-match
required after the prefix -- `claude-opus-4` must not match a hypothetical
`claude-opus-45`. A model with no entry is UNPRICED: `rates_for_model`
returns None and `cost_usd` returns None -- never 0 (CLAUDE.md rule #12: a
missing price must not render as a free call).

`RATES_AS_OF` records the date this table was last checked against
https://platform.claude.com/docs/en/about-claude/pricing -- surfaced by the
API (`serve.py`'s `cost_basis.rates_as_of`) and the UI so a reader can judge
how current the costing basis is before trusting a dollar figure.
`claude-sonnet-5` carries Anthropic's INTRODUCTORY pricing (in effect through
2026-08-31); update `PRICES`, `RATES_AS_OF`, and
`PricingTest.test_longest_prefix_wins` together when standard pricing takes
effect.
"""

from __future__ import annotations

from typing import Optional

# Last date this table was checked against Anthropic's published pricing page.
# A dated, hand-maintained value -- bump it (and PRICES) together whenever
# the rates are re-verified, so a reader can tell a fresh table from a stale
# one instead of trusting an undated number.
RATES_AS_OF = "2026-08-02"

# Per-MTok USD list rates: input / cache_read / cache_write (5m) / output.
PRICES: dict[str, dict[str, float]] = {
    "claude-haiku-4-5": {"input": 1.00, "cache_read": 0.10, "cache_write_5m": 1.25, "output": 5.00},
    "claude-sonnet-5": {"input": 2.00, "cache_read": 0.20, "cache_write_5m": 2.50, "output": 10.00},
    "claude-opus-4": {"input": 15.00, "cache_read": 1.50, "cache_write_5m": 18.75, "output": 75.00},
    "claude-opus-5": {"input": 5.00, "cache_read": 0.50, "cache_write_5m": 6.25, "output": 25.00},
    "claude-fable-5": {"input": 10.00, "cache_read": 1.00, "cache_write_5m": 12.50, "output": 50.00},
}

_MTOK = 1_000_000


def rates_for_model(model: str) -> Optional[dict[str, float]]:
    """Return the rate dict for a model id by longest-prefix match, or None.

    The returned dict is normalized to keys input/cache_read/cache_write/output.
    A prefix only matches at a `-` boundary (or exactly), so `claude-opus-4`
    cannot match a hypothetical `claude-opus-45`.
    """
    best: Optional[str] = None
    for prefix in PRICES:
        if model == prefix or model.startswith(prefix + "-"):
            if best is None or len(prefix) > len(best):
                best = prefix
    if best is None:
        return None
    r = PRICES[best]
    return {
        "input": r["input"],
        "cache_read": r["cache_read"],
        "cache_write": r["cache_write_5m"],
        "output": r["output"],
    }


def cost_usd(
    model: str,
    input_tokens: int,
    cache_read: int,
    cache_write: int,
    output_tokens: int,
) -> Optional[float]:
    """List-rate cost ESTIMATE for one API call, or None if the model is unpriced.

    Not a bill: pure arithmetic over `PRICES` (see module docstring for what
    it excludes and `RATES_AS_OF` for how current the table is).
    """
    rates = rates_for_model(model)
    if rates is None:
        return None
    return (
        input_tokens * rates["input"]
        + cache_read * rates["cache_read"]
        + cache_write * rates["cache_write"]
        + output_tokens * rates["output"]
    ) / _MTOK
