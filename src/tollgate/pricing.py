"""Token normalization and per-request cost attribution.

Three things make an LLM bill hard to reconstruct after the fact, and this
module models all three.

**Providers report usage differently.** Anthropic's `input_tokens` /
`cache_creation_input_tokens`, OpenAI chat's `prompt_tokens` (which *includes*
`prompt_tokens_details.cached_tokens`), the Responses API's third variant.
`normalize_usage` collapses them into one shape so a mixed log is summable.

**Rates change, and a log outlives them.** Prices are effective-dated: every
model maps to a list of rate periods, and a request is priced at the rate in
force on the day it was made. Claude Sonnet 5's introductory rate expires on
2026-09-01 without anyone editing a table; a February 2026 log with a 300K-token
Sonnet 4.6 call still prices at the long-context premium that was actually
billed, even though Anthropic removed that premium on 2026-03-13. Re-running a
report is therefore reproducible rather than merely repeatable.

**A token's price depends on how it was used.** Cache reads bill at 0.1x input;
cache writes at 1.25x for a 5-minute TTL and 2x for an hour; a request over a
model's long-context threshold reprices the *entire* request; and a service tier
scales the whole thing.

An unknown model or an unmodelled service tier yields ``None`` rather than a
number — a missing price should read as missing, not as free. Override or extend
the table with a JSON file:

    TOLLGATE_PRICING=~/prices.json tollgate report

    {"gpt-6": {"input": 1.25, "output": 10.0, "cache_read_mult": 0.1}}
"""

from __future__ import annotations

import json
import os
from bisect import bisect_right
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

#: Standard-tier rates, verified 2026-07-28 against both providers' published
#: pricing. Anthropic rates are first-party API list prices; Bedrock and Vertex
#: are partner-operated and differ.
PRICES_AS_OF = "2026-07-28"

#: Earlier than any capture log, used as the opening bound of a rate period.
_DAWN = date(2024, 1, 1)

#: Anthropic removed the >200K-token long-context premium (2x input,
#: 1.5x output) on this date, moving to flat pricing across the full 1M window.
_ANTHROPIC_FLAT_LONG_CONTEXT = date(2026, 3, 13)

#: Claude Sonnet 5 introductory pricing runs through 2026-08-31.
_SONNET_5_STANDARD = date(2026, 9, 1)


@dataclass(frozen=True)
class Rate:
    """What a million tokens costs, and how usage kinds scale off that."""

    input: float
    output: float
    cache_read_mult: float = 0.1
    cache_write_5m_mult: float = 1.25
    cache_write_1h_mult: float = 2.0
    #: Prompt size at or above which the whole request reprices, if any.
    long_context_threshold: int | None = None
    long_context_input_mult: float = 1.0
    long_context_output_mult: float = 1.0


@dataclass(frozen=True)
class RatePeriod:
    effective_from: date
    rate: Rate


#: Service tiers scale the whole request. `priority` is deliberately absent:
#: published figures disagree (2x vs 2.5x), and a tier priced from a guess is
#: worse than one that reports itself unknown.
SERVICE_TIER_MULTS = {
    "default": 1.0,
    "auto": 1.0,
    "standard": 1.0,
    "scale": 1.0,
    "flex": 0.5,
    "batch": 0.5,
}


def _flat(input_: float, output: float, **kw: Any) -> list[RatePeriod]:
    """A model whose price has never changed."""
    return [RatePeriod(_DAWN, Rate(input_, output, **kw))]


# -- Anthropic ---------------------------------------------------------------

_ANTHROPIC: dict[str, list[RatePeriod]] = {
    "claude-fable-5": _flat(10.0, 50.0),
    "claude-mythos-5": _flat(10.0, 50.0),
    "claude-mythos-preview": _flat(10.0, 50.0),
    "claude-opus-5": _flat(5.0, 25.0),
    "claude-opus-4-8": _flat(5.0, 25.0),
    "claude-opus-4-7": _flat(5.0, 25.0),
    "claude-opus-4-5": _flat(5.0, 25.0),
    "claude-opus-4-1": _flat(15.0, 75.0),
    "claude-sonnet-4-5": _flat(3.0, 15.0),
    "claude-haiku-4-5": _flat(1.0, 5.0),
    # The two 1M-context models that predate flat long-context pricing.
    "claude-opus-4-6": [
        RatePeriod(
            _DAWN,
            Rate(
                5.0,
                25.0,
                long_context_threshold=200_000,
                long_context_input_mult=2.0,
                long_context_output_mult=1.5,
            ),
        ),
        RatePeriod(_ANTHROPIC_FLAT_LONG_CONTEXT, Rate(5.0, 25.0)),
    ],
    "claude-sonnet-4-6": [
        RatePeriod(
            _DAWN,
            Rate(
                3.0,
                15.0,
                long_context_threshold=200_000,
                long_context_input_mult=2.0,
                long_context_output_mult=1.5,
            ),
        ),
        RatePeriod(_ANTHROPIC_FLAT_LONG_CONTEXT, Rate(3.0, 15.0)),
    ],
    # Introductory pricing expires on its own, with no table edit.
    "claude-sonnet-5": [
        RatePeriod(_DAWN, Rate(2.0, 10.0)),
        RatePeriod(_SONNET_5_STANDARD, Rate(3.0, 15.0)),
    ],
}


# -- OpenAI ------------------------------------------------------------------

#: (input, output, cached input). OpenAI prices the cached read directly rather
#: than as one flat discount — the gpt-5 line reads at 90% off, gpt-4.1 and the
#: o-series at 75%, gpt-4o at 50% — so the multiplier is derived per model
#: instead of assumed per provider. Nothing is charged to write.
_OPENAI_SIMPLE = {
    "gpt-5.5-pro": (30.00, 180.00, 3.00),
    "gpt-5.5": (5.00, 30.00, 0.50),
    "gpt-5.4-pro": (30.00, 180.00, 3.00),
    "gpt-5.4-mini": (0.75, 4.50, 0.075),
    "gpt-5.4-nano": (0.20, 1.25, 0.02),
    "gpt-5.4": (2.50, 15.00, 0.25),
    "gpt-5.2": (1.75, 14.00, 0.175),
    "gpt-5.1": (1.25, 10.00, 0.125),
    "gpt-5-mini": (0.25, 2.00, 0.025),
    "gpt-5-nano": (0.05, 0.40, 0.005),
    "gpt-5": (1.25, 10.00, 0.125),
    "gpt-4.1-nano": (0.10, 0.40, 0.025),
    "gpt-4.1-mini": (0.40, 1.60, 0.10),
    "gpt-4.1": (2.00, 8.00, 0.50),
    "gpt-4o-mini": (0.15, 0.60, 0.075),
    "gpt-4o": (2.50, 10.00, 1.25),
    "o4-mini": (1.10, 4.40, 0.275),
    "o3": (2.00, 8.00, 0.50),
}

#: The gpt-5.6 line reprices the entire request above 272K input tokens:
#: 2x input, 1.5x output. Same (input, output, cached) shape as above.
_OPENAI_LONG_CONTEXT = {
    "gpt-5.6-sol": (5.00, 30.00, 0.50),
    "gpt-5.6-terra": (2.50, 15.00, 0.25),
    "gpt-5.6-luna": (1.00, 6.00, 0.10),
}

_GPT_56_THRESHOLD = 272_000


def _build_table() -> dict[str, list[RatePeriod]]:
    table: dict[str, list[RatePeriod]] = dict(_ANTHROPIC)

    for model, (inp, out, cached) in _OPENAI_SIMPLE.items():
        table[model] = _flat(
            inp,
            out,
            cache_read_mult=cached / inp,
            cache_write_5m_mult=0.0,
            cache_write_1h_mult=0.0,
        )
    for model, (inp, out, cached) in _OPENAI_LONG_CONTEXT.items():
        table[model] = _flat(
            inp,
            out,
            cache_read_mult=cached / inp,
            cache_write_5m_mult=0.0,
            cache_write_1h_mult=0.0,
            long_context_threshold=_GPT_56_THRESHOLD,
            long_context_input_mult=2.0,
            long_context_output_mult=1.5,
        )

    override = os.environ.get("TOLLGATE_PRICING")
    if override:
        with open(os.path.expanduser(override)) as f:
            for model, spec in json.load(f).items():
                table[model] = _flat(
                    float(spec["input"]),
                    float(spec["output"]),
                    cache_read_mult=float(spec.get("cache_read_mult", 0.1)),
                    cache_write_5m_mult=float(spec.get("cache_write_5m_mult", 1.25)),
                    cache_write_1h_mult=float(spec.get("cache_write_1h_mult", 2.0)),
                    long_context_threshold=spec.get("long_context_threshold"),
                    long_context_input_mult=float(spec.get("long_context_input_mult", 1.0)),
                    long_context_output_mult=float(spec.get("long_context_output_mult", 1.0)),
                )

    # Newest period last, so a bisect finds the one in force on a given day.
    return {m: sorted(p, key=lambda rp: rp.effective_from) for m, p in table.items()}


PRICES = _build_table()


def as_of(value: Any) -> date:
    """Coerce a record timestamp to a date, falling back to today."""
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value).date()
        except ValueError:
            pass
    return datetime.now(timezone.utc).date()


def price_for(model: str | None, at: Any = None) -> Rate | None:
    """The rate in force for `model` on day `at`.

    Model lookup is exact, then longest prefix, so a dated snapshot inherits
    its alias — ``claude-haiku-4-5-20251001`` prices as ``claude-haiku-4-5``,
    and ``gpt-5.6-sol-2026-07-01`` as ``gpt-5.6-sol`` rather than colliding
    with the far cheaper ``gpt-5``.
    """
    if not model:
        return None
    periods = PRICES.get(model)
    if periods is None:
        matches = [k for k in PRICES if model.startswith(k)]
        if not matches:
            return None
        periods = PRICES[max(matches, key=len)]

    when = as_of(at)
    index = bisect_right([p.effective_from for p in periods], when)
    if index == 0:
        # Priced from before the first known period; use the earliest rate.
        return periods[0].rate
    return periods[index - 1].rate


# -- usage normalization -----------------------------------------------------

USAGE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "cache_write_5m_tokens",
    "cache_write_1h_tokens",
)

_EMPTY_USAGE = dict.fromkeys(USAGE_FIELDS, 0)


def _int(value: Any) -> int:
    return value if isinstance(value, int) else 0


def _with_write_split(usage: dict[str, int], breakdown: Any) -> dict[str, int]:
    """Split cache writes by TTL, since an hour costs 2x and five minutes 1.25x.

    Anthropic reports the split under `usage.cache_creation`. Without it, the
    whole write is attributed to the 5-minute rate — the cheaper of the two, so
    a 1h-TTL workload on an older log is under-costed rather than over.
    """
    total = usage["cache_write_tokens"]
    if isinstance(breakdown, dict):
        five = _int(breakdown.get("ephemeral_5m_input_tokens"))
        hour = _int(breakdown.get("ephemeral_1h_input_tokens"))
        if five or hour:
            usage["cache_write_5m_tokens"] = five
            usage["cache_write_1h_tokens"] = hour
            usage["cache_write_tokens"] = five + hour
            return usage
    usage["cache_write_5m_tokens"] = total
    usage["cache_write_1h_tokens"] = 0
    return usage


def normalize_usage(response: dict[str, Any] | None) -> dict[str, int]:
    """Collapse any provider's usage block into one shape."""
    if not isinstance(response, dict):
        return dict(_EMPTY_USAGE)
    usage = response.get("usage")
    if not isinstance(usage, dict):
        return dict(_EMPTY_USAGE)

    if "prompt_tokens" in usage:  # OpenAI chat completions
        details = usage.get("prompt_tokens_details") or {}
        cached = _int(details.get("cached_tokens")) if isinstance(details, dict) else 0
        return dict(
            _EMPTY_USAGE,
            # OpenAI reports prompt_tokens inclusive of the cached portion.
            input_tokens=max(_int(usage.get("prompt_tokens")) - cached, 0),
            output_tokens=_int(usage.get("completion_tokens")),
            cache_read_tokens=cached,
        )

    details = usage.get("input_tokens_details") or {}
    cached = _int(details.get("cached_tokens")) if isinstance(details, dict) else 0
    if cached:  # OpenAI Responses API
        return dict(
            _EMPTY_USAGE,
            input_tokens=max(_int(usage.get("input_tokens")) - cached, 0),
            output_tokens=_int(usage.get("output_tokens")),
            cache_read_tokens=cached,
        )

    # Anthropic — input_tokens is already exclusive of the cache fields.
    normalized = dict(
        _EMPTY_USAGE,
        input_tokens=_int(usage.get("input_tokens")),
        output_tokens=_int(usage.get("output_tokens")),
        cache_read_tokens=_int(usage.get("cache_read_input_tokens")),
        cache_write_tokens=_int(usage.get("cache_creation_input_tokens")),
    )
    return _with_write_split(normalized, usage.get("cache_creation"))


def prompt_tokens(usage: dict[str, int]) -> int:
    """Everything that counted as prompt, which is what a threshold measures."""
    return (
        usage.get("input_tokens", 0)
        + usage.get("cache_read_tokens", 0)
        + usage.get("cache_write_tokens", 0)
    )


def response_model(request: dict[str, Any], response: dict[str, Any] | None) -> str | None:
    """The model that actually served the request, falling back to the ask.

    A server-side fallback can serve a different model than the one requested,
    and the response is the authority on which one billed.
    """
    if isinstance(response, dict) and isinstance(response.get("model"), str):
        return response["model"]
    model = request.get("model")
    return model if isinstance(model, str) else None


def service_tier(request: dict[str, Any], response: dict[str, Any] | None) -> str | None:
    """The tier that actually served the request, not the one asked for.

    OpenAI echoes `service_tier` on the response; `auto` in a request can be
    served as `default` or `flex`, so the response wins where present.
    """
    for source in (response, request):
        if isinstance(source, dict) and isinstance(source.get("service_tier"), str):
            return source["service_tier"]
    return None


def cost_usd(
    model: str | None,
    usage: dict[str, int],
    *,
    at: Any = None,
    tier: str | None = None,
) -> float | None:
    """Dollar cost of one request, or None if it cannot be priced honestly.

    None means one of: unknown model, or a service tier whose multiplier isn't
    modelled. Both are cases where a number would be a guess.
    """
    rate = price_for(model, at)
    if rate is None:
        return None

    tier_mult = 1.0
    if tier is not None:
        if tier not in SERVICE_TIER_MULTS:
            return None
        tier_mult = SERVICE_TIER_MULTS[tier]

    input_rate = rate.input
    output_rate = rate.output
    if (
        rate.long_context_threshold is not None
        and prompt_tokens(usage) > rate.long_context_threshold
    ):
        # The premium reprices the whole request, not just the excess.
        input_rate *= rate.long_context_input_mult
        output_rate *= rate.long_context_output_mult

    write_5m = usage.get("cache_write_5m_tokens", 0)
    write_1h = usage.get("cache_write_1h_tokens", 0)
    if not write_5m and not write_1h:
        # A usage dict without the TTL split — a log captured before the split
        # existed, or one built by hand. Bill it all at the 5-minute rate
        # rather than silently treating the writes as free.
        write_5m = usage.get("cache_write_tokens", 0)

    per_in = input_rate / 1_000_000
    per_out = output_rate / 1_000_000
    total = (
        usage.get("input_tokens", 0) * per_in
        + usage.get("cache_read_tokens", 0) * per_in * rate.cache_read_mult
        + write_5m * per_in * rate.cache_write_5m_mult
        + write_1h * per_in * rate.cache_write_1h_mult
        + usage.get("output_tokens", 0) * per_out
    )
    return total * tier_mult


def rate_card(model: str, at: Any = None) -> dict[str, Any] | None:
    """The effective per-MTok rates for a model on a given day, for display."""
    rate = price_for(model, at)
    if rate is None:
        return None
    return {
        "model": model,
        "as_of": as_of(at).isoformat(),
        "input": rate.input,
        "output": rate.output,
        "cache_read": rate.input * rate.cache_read_mult,
        "cache_write_5m": rate.input * rate.cache_write_5m_mult,
        "cache_write_1h": rate.input * rate.cache_write_1h_mult,
        "long_context_threshold": rate.long_context_threshold,
    }


__all__ = [
    "PRICES",
    "PRICES_AS_OF",
    "Rate",
    "RatePeriod",
    "SERVICE_TIER_MULTS",
    "USAGE_FIELDS",
    "as_of",
    "cost_usd",
    "normalize_usage",
    "price_for",
    "prompt_tokens",
    "rate_card",
    "response_model",
    "service_tier",
]
