"""Token -> USD conversion.

Rates are USD per 1,000,000 tokens, sourced from Anthropic's published API
pricing. Edit this table when pricing changes — nothing else needs to know
about rates.

Cache pricing follows the standard multipliers on the *input* rate:
  * cache read   ~= 0.1x  input
  * cache write  ~= 1.25x input (5-minute TTL)  /  2.0x input (1-hour TTL)

The transcripts record 5m vs 1h cache creation separately
(``usage.cache_creation.ephemeral_5m_input_tokens`` / ``ephemeral_1h_...``),
so we bill each at its own multiplier when the breakdown is present and fall
back to the 5m rate otherwise.
"""

from __future__ import annotations

CACHE_READ_MULTIPLIER = 0.10
CACHE_WRITE_5M_MULTIPLIER = 1.25
CACHE_WRITE_1H_MULTIPLIER = 2.00

# model id -> (input $/MTok, output $/MTok)
PRICING: dict[str, tuple[float, float]] = {
    "claude-fable-5-1": (10.00, 50.00),
    "claude-fable-5": (10.00, 50.00),
    "claude-mythos-5": (10.00, 50.00),
    "claude-opus-5-5": (4.00, 20.00),
    "claude-opus-5": (5.00, 25.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-opus-4-7": (5.00, 25.00),
    "claude-opus-4-6": (5.00, 25.00),
    "claude-opus-4-5": (5.00, 25.00),
    "claude-opus-4-1": (15.00, 75.00),
    "claude-opus-4-0": (15.00, 75.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-sonnet-4-5": (3.00, 15.00),
    "claude-sonnet-4-0": (3.00, 15.00),
    "claude-haiku-5-5": (0.10, 0.50),  # prompts up to 100K tokens
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-3-5-haiku": (0.80, 4.00),
    "claude-3-haiku": (0.25, 1.25),
}

# Models billed at a premium when Claude Code runs them in fast mode.
FAST_MODE_PRICING: dict[str, tuple[float, float]] = {
    "claude-opus-5-5": (8.00, 40.00),
    "claude-opus-5": (10.00, 50.00),
    "claude-opus-4-8": (10.00, 50.00),
}

# Cache-read $/MTok where it is not the 0.1x input multiplier.
CACHE_READ_RATES: dict[str, float] = {
    "claude-opus-5-5": 0.20,
    "claude-fable-5-1": 0.25,
}

DEFAULT_RATES = (3.00, 15.00)  # unknown model -> assume Sonnet-tier

_UNKNOWN_MODELS: set[str] = set()


def normalize_model(model: str | None) -> str:
    """Strip the dated-snapshot suffix so `claude-haiku-4-5-20251001` matches."""
    if not model:
        return "unknown"
    model = model.strip()
    parts = model.rsplit("-", 1)
    if len(parts) == 2 and parts[1].isdigit() and len(parts[1]) == 8:
        return parts[0]
    return model


def rates_for(model: str | None, fast: bool = False) -> tuple[float, float]:
    key = normalize_model(model)
    if fast and key in FAST_MODE_PRICING:
        return FAST_MODE_PRICING[key]
    if key in PRICING:
        return PRICING[key]
    if key not in ("unknown", "<synthetic>"):
        _UNKNOWN_MODELS.add(key)
    return DEFAULT_RATES


def unknown_models() -> list[str]:
    """Models seen during parsing that had no pricing entry (shown in the UI)."""
    return sorted(_UNKNOWN_MODELS)


def cache_read_rate(model: str | None, in_rate: float) -> float:
    return CACHE_READ_RATES.get(normalize_model(model), in_rate * CACHE_READ_MULTIPLIER)


def cost_usd(
    model: str | None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
    cache_creation_5m: int | None = None,
    cache_creation_1h: int | None = None,
    fast: bool = False,
) -> float:
    """Cost of a single API call, in USD."""
    in_rate, out_rate = rates_for(model, fast=fast)

    if cache_creation_5m is None and cache_creation_1h is None:
        write_cost = cache_creation_tokens * in_rate * CACHE_WRITE_5M_MULTIPLIER
    else:
        write_cost = (
            (cache_creation_5m or 0) * in_rate * CACHE_WRITE_5M_MULTIPLIER
            + (cache_creation_1h or 0) * in_rate * CACHE_WRITE_1H_MULTIPLIER
        )

    total = (
        input_tokens * in_rate
        + output_tokens * out_rate
        + cache_read_tokens * cache_read_rate(model, in_rate)
        + write_cost
    )
    return total / 1_000_000.0


def pricing_table() -> list[dict]:
    """Rendered on the /analytics page so the rates in use are always visible."""
    rows = []
    for model, (inp, out) in sorted(PRICING.items()):
        rows.append(
            {
                "model": model,
                "input": inp,
                "output": out,
                "cache_read": round(cache_read_rate(model, inp), 4),
                "cache_write_5m": round(inp * CACHE_WRITE_5M_MULTIPLIER, 4),
                "cache_write_1h": round(inp * CACHE_WRITE_1H_MULTIPLIER, 4),
            }
        )
    return rows
