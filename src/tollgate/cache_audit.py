"""Find prompt caching that is silently not working.

Prompt caching fails quietly. There is no error when a cache never hits — the
request succeeds, the response is correct, and the only symptom is a bill that
is several times larger than it needed to be. The usual causes are a missing
`cache_control` breakpoint, or a "silent invalidator": a timestamp, a UUID, or
an unsorted `json.dumps` somewhere in the prompt prefix, which changes the
cached bytes on every single call.

Tollgate holds the two things needed to detect this — a fingerprint of each
prompt and the provider's own cache counters — so it can name the failure
instead of leaving it to show up on an invoice.

Three findings, in the order they are worth acting on:

  never_cached      The same prompt, byte for byte, sent repeatedly with no
                    cache reads at all. Nothing is being cached; the usual
                    cause is a missing breakpoint.
  invalidated       Cache writes with almost no reads, across prompts that are
                    all slightly different. The prefix is changing every call,
                    so each write is paid for and then thrown away.
  partial           Caching works but reads cover only part of a large prompt —
                    the breakpoint is probably placed too early.
"""

from __future__ import annotations

from typing import Any

from .pricing import price_for, prompt_tokens

#: Below this a prompt is too small to cache on any current model, so a zero
#: hit rate says nothing.
MIN_CACHEABLE_TOKENS = 512

#: A prompt seen fewer times than this has no cache history to judge.
MIN_CALLS = 3

#: Read share below which caching is considered not to be working.
COLD_HIT_RATE = 0.05

#: Read share below which a working cache still looks under-used.
PARTIAL_HIT_RATE = 0.5


def _hit_rate(usage: dict[str, int]) -> float:
    total = prompt_tokens(usage)
    return usage.get("cache_read_tokens", 0) / total if total else 0.0


def _savings(model: str | None, tokens: int, calls: int, at: Any) -> float | None:
    """What the uncached prompt tokens would have cost at the cache-read rate.

    The counterfactual is deliberately conservative: the first call always pays
    to write, so only the remaining `calls - 1` could have been reads.
    """
    rate = price_for(model, at)
    if rate is None or calls < 2:
        return None
    per_token = rate.input / 1_000_000
    return tokens * per_token * (1 - rate.cache_read_mult)


def audit(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group priced records by prompt and report caching that isn't working.

    Expects records already run through `replay.reprice`, so usage and cost
    reflect the current price table.
    """
    by_prompt: dict[Any, list[dict[str, Any]]] = {}
    for rec in records:
        if rec.get("status", 200) != 200:
            continue
        by_prompt.setdefault(rec.get("prompt_sha"), []).append(rec)

    findings: list[dict[str, Any]] = []

    # 1. Identical prompts, repeated, never cached.
    for sha, recs in by_prompt.items():
        if len(recs) < MIN_CALLS:
            continue
        usage = _sum_usage(recs)
        if prompt_tokens(usage) / len(recs) < MIN_CACHEABLE_TOKENS:
            continue
        rate = _hit_rate(usage)
        if rate >= PARTIAL_HIT_RATE:
            continue
        model = recs[0].get("model")
        repeated = prompt_tokens(usage) - usage.get("cache_read_tokens", 0)
        # Only the calls after the first could have been served from cache.
        recoverable = repeated * (len(recs) - 1) / len(recs)
        findings.append(
            {
                "finding": "never_cached" if rate < COLD_HIT_RATE else "partial",
                "prompt_sha": sha,
                "model": model,
                "calls": len(recs),
                "prompt_tokens": prompt_tokens(usage),
                "hit_rate": round(rate, 3),
                "spent_usd": _sum_cost(recs),
                "recoverable_usd": _savings(model, recoverable, len(recs), _first_ts(recs)),
                "detail": (
                    f"the same prompt ran {len(recs)} times with a "
                    f"{rate:.0%} cache hit rate"
                ),
            }
        )

    # 2. Prompts that are all slightly different, paying to write a cache that
    #    is never read — the signature of a changing prefix.
    for model, recs in _group(records, "model").items():
        live = [r for r in recs if r.get("status", 200) == 200]
        if len(live) < MIN_CALLS:
            continue
        usage = _sum_usage(live)
        writes = usage.get("cache_write_tokens", 0)
        reads = usage.get("cache_read_tokens", 0)
        distinct = len({r.get("prompt_sha") for r in live})
        # Writing on nearly every call, reading on nearly none, and never
        # repeating a prompt exactly.
        if not writes or reads > writes * 0.25 or distinct < len(live) * 0.9:
            continue
        findings.append(
            {
                "finding": "invalidated",
                "prompt_sha": None,
                "model": model,
                "calls": len(live),
                "prompt_tokens": prompt_tokens(usage),
                "hit_rate": round(_hit_rate(usage), 3),
                "spent_usd": _sum_cost(live),
                "recoverable_usd": _savings(model, writes, len(live), _first_ts(live)),
                "detail": (
                    f"{distinct} distinct prompts over {len(live)} calls wrote "
                    f"{writes:,} tokens to cache and read back {reads:,} — the "
                    "prefix appears to change every call"
                ),
            }
        )

    findings.sort(key=lambda f: f["recoverable_usd"] or 0, reverse=True)
    return findings


def _group(records: list[dict[str, Any]], key: str) -> dict[Any, list[dict[str, Any]]]:
    grouped: dict[Any, list[dict[str, Any]]] = {}
    for rec in records:
        grouped.setdefault(rec.get(key), []).append(rec)
    return grouped


def _sum_usage(records: list[dict[str, Any]]) -> dict[str, int]:
    total: dict[str, int] = {}
    for rec in records:
        for field, value in (rec.get("usage") or {}).items():
            total[field] = total.get(field, 0) + value
    return total


def _sum_cost(records: list[dict[str, Any]]) -> float | None:
    costs = [r.get("cost_usd") for r in records]
    return None if any(c is None for c in costs) else sum(costs)


def _first_ts(records: list[dict[str, Any]]) -> Any:
    return records[0].get("timestamp")
