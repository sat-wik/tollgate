"""Token normalization and per-request cost attribution.

Providers report usage under different names — Anthropic's `input_tokens` /
`cache_creation_input_tokens`, OpenAI chat's `prompt_tokens` /
`prompt_tokens_details.cached_tokens`, the Responses API's `input_tokens` /
`input_tokens_details.cached_tokens`. `normalize_usage` collapses all three into
one shape so a Tollgate log can be summed across providers.

Prices are per million tokens. Cached tokens bill at a multiple of the input
rate (Anthropic: 0.1x on read, 1.25x on a 5-minute write), so cost is:

    input * in + cache_read * in * read_mult
        + cache_write * in * write_mult + output * out

An unknown model yields ``None`` rather than 0.0 — a missing price should read
as missing, not free. Override or extend the table with a JSON file:

    TOLLGATE_PRICING=~/prices.json tollgate report

    {"gpt-5": {"input": 1.25, "output": 10.0, "cache_read_mult": 0.1}}
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

#: Prices are per million tokens, current as of 2026-07.
#: Anthropic rates are first-party API list prices; Bedrock/Vertex are
#: partner-operated and differ. Claude Sonnet 5 carries an introductory
#: $2.00/$10.00 rate through 2026-08-31 — the table lists the standard rate, so
#: override it if you want the discount reflected.
PRICES_AS_OF = "2026-07"

_ANTHROPIC = {
    "claude-fable-5": (10.0, 50.0),
    "claude-mythos-5": (10.0, 50.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-opus-4-6": (5.0, 25.0),
    "claude-opus-4-5": (5.0, 25.0),
    "claude-opus-4-1": (15.0, 75.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-sonnet-4-5": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
}

#: OpenAI list prices change often and are not tracked as closely here as the
#: Anthropic table. Verify against openai.com/api/pricing for billing-grade
#: numbers, or override via TOLLGATE_PRICING.
_OPENAI = {
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.0),
    "gpt-4.1-nano": (0.10, 0.40),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1": (2.00, 8.00),
    "o4-mini": (1.10, 4.40),
    "o3-mini": (1.10, 4.40),
    "o3": (2.00, 8.00),
}


@dataclass(frozen=True)
class Price:
    input_usd_per_mtok: float
    output_usd_per_mtok: float
    cache_read_mult: float = 0.1
    cache_write_mult: float = 1.25


def _build_table() -> dict[str, Price]:
    table = {m: Price(i, o) for m, (i, o) in _ANTHROPIC.items()}
    # OpenAI discounts cached input rather than charging to write it.
    table.update(
        {
            m: Price(i, o, cache_read_mult=0.5, cache_write_mult=0.0)
            for m, (i, o) in _OPENAI.items()
        }
    )
    override = os.environ.get("TOLLGATE_PRICING")
    if override:
        with open(os.path.expanduser(override)) as f:
            for model, spec in json.load(f).items():
                table[model] = Price(
                    float(spec["input"]),
                    float(spec["output"]),
                    float(spec.get("cache_read_mult", 0.1)),
                    float(spec.get("cache_write_mult", 1.25)),
                )
    return table


PRICES = _build_table()


def price_for(model: str | None) -> Price | None:
    """Exact match, then longest prefix — so dated snapshots inherit the alias.

    ``claude-haiku-4-5-20251001`` prices as ``claude-haiku-4-5``.
    """
    if not model:
        return None
    if model in PRICES:
        return PRICES[model]
    matches = [k for k in PRICES if model.startswith(k)]
    if not matches:
        return None
    return PRICES[max(matches, key=len)]


# -- usage normalization -----------------------------------------------------


def _int(value: Any) -> int:
    return value if isinstance(value, int) else 0


def normalize_usage(response: dict[str, Any] | None) -> dict[str, int]:
    """Collapse any provider's usage block into one four-field shape."""
    empty = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
    }
    if not isinstance(response, dict):
        return empty
    usage = response.get("usage")
    if not isinstance(usage, dict):
        return empty

    if "prompt_tokens" in usage:  # OpenAI chat completions
        details = usage.get("prompt_tokens_details") or {}
        cached = _int(details.get("cached_tokens")) if isinstance(details, dict) else 0
        return {
            # OpenAI reports prompt_tokens inclusive of the cached portion.
            "input_tokens": max(_int(usage.get("prompt_tokens")) - cached, 0),
            "output_tokens": _int(usage.get("completion_tokens")),
            "cache_read_tokens": cached,
            "cache_write_tokens": 0,
        }

    details = usage.get("input_tokens_details") or {}
    cached = _int(details.get("cached_tokens")) if isinstance(details, dict) else 0
    if cached:  # OpenAI Responses API
        return {
            "input_tokens": max(_int(usage.get("input_tokens")) - cached, 0),
            "output_tokens": _int(usage.get("output_tokens")),
            "cache_read_tokens": cached,
            "cache_write_tokens": 0,
        }

    # Anthropic (and the Responses API with no cache hit) — input_tokens is
    # already exclusive of the cache fields.
    return {
        "input_tokens": _int(usage.get("input_tokens")),
        "output_tokens": _int(usage.get("output_tokens")),
        "cache_read_tokens": _int(usage.get("cache_read_input_tokens")),
        "cache_write_tokens": _int(usage.get("cache_creation_input_tokens")),
    }


def response_model(request: dict[str, Any], response: dict[str, Any] | None) -> str | None:
    """The model that actually served the request, falling back to the ask.

    A server-side fallback can serve a different model than the one requested,
    and the response is the authority on which one billed.
    """
    if isinstance(response, dict) and isinstance(response.get("model"), str):
        return response["model"]
    model = request.get("model")
    return model if isinstance(model, str) else None


def cost_usd(model: str | None, usage: dict[str, int]) -> float | None:
    """Dollar cost of one request, or None if the model has no known price."""
    price = price_for(model)
    if price is None:
        return None
    per_token_in = price.input_usd_per_mtok / 1_000_000
    per_token_out = price.output_usd_per_mtok / 1_000_000
    return (
        usage["input_tokens"] * per_token_in
        + usage["cache_read_tokens"] * per_token_in * price.cache_read_mult
        + usage["cache_write_tokens"] * per_token_in * price.cache_write_mult
        + usage["output_tokens"] * per_token_out
    )
