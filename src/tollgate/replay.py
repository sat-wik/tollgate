"""Replay: turn a capture log into cost and latency analysis you can rerun.

Two modes, both driven off the raw request/response pairs in the log.

**Offline** (`reprice`) recomputes usage and cost from the stored pairs without
touching the network. It is deterministic: the same log and the same price
table always produce the same numbers, so a cost report is reproducible months
later and re-prices correctly if the table changes.

**Live** (`replay_live`) re-issues the logged requests against the real
provider, optionally swapping the model or editing the prompt, and diffs the
result against the recorded baseline. That is what makes "did this model change
cost us anything?" a measurement rather than an estimate. Requests are replayed
non-streamed so the comparison sees one complete response body either way; the
recorded baseline is already reconstructed to the same shape.

Live replay never reads credentials from the log — Tollgate does not store
them. It takes ANTHROPIC_API_KEY / OPENAI_API_KEY from the environment.
"""

from __future__ import annotations

import copy
import os
import time
from typing import Any, Callable, Iterable, Iterator

import httpx

from .pricing import cost_usd, normalize_usage, response_model, service_tier
from .record import build_record

UPSTREAMS = {
    "openai": os.environ.get("TOLLGATE_UPSTREAM_OPENAI", "https://api.openai.com"),
    "anthropic": os.environ.get("TOLLGATE_UPSTREAM_ANTHROPIC", "https://api.anthropic.com"),
}

ANTHROPIC_VERSION = os.environ.get("TOLLGATE_ANTHROPIC_VERSION", "2023-06-01")


# -- offline: deterministic recompute ----------------------------------------


def reprice_iter(
    records: Iterable[dict[str, Any]], *, keep_bodies: bool = True
) -> Iterator[dict[str, Any]]:
    """Recompute usage and cost for each record from its raw pair.

    Pure: no network, no clock, no randomness. Latency is carried through from
    the capture (it can only be measured live), everything else is derived.

    `keep_bodies=False` drops the request and response once they have been
    priced. Aggregation never reads them again, and they are the whole reason a
    capture log is large — dropping them keeps a report's memory proportional
    to the number of calls rather than the volume of traffic.
    """
    for rec in records:
        request = rec.get("request") or {}
        response = rec.get("response")
        model = response_model(request, response)
        usage = normalize_usage(response)
        tier = service_tier(request, response)
        priced = dict(rec)
        priced["model"] = model
        priced["usage"] = usage
        priced["service_tier"] = tier
        priced["cost_usd"] = (
            0.0
            if rec.get("status", 200) != 200
            # Priced at the rate in force when the call was made, not today's.
            else cost_usd(model, usage, at=rec.get("timestamp"), tier=tier)
        )
        if not keep_bodies:
            priced.pop("request", None)
            priced.pop("response", None)
        yield priced


def reprice(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """`reprice_iter` collected into a list, with the raw pairs intact."""
    return list(reprice_iter(records))


def percentile(values: list[float], pct: float) -> float | None:
    """Nearest-rank percentile. Small samples are the norm here, so no interpolation."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, min(len(ordered), int(-(-pct / 100 * len(ordered) // 1))))
    return ordered[rank - 1]


def summarize(
    records: Iterable[dict[str, Any]], group_by: str = "model"
) -> list[dict[str, Any]]:
    """Aggregate priced records into one row per group, most expensive first.

    Accepts an iterator, so a report can stream a log it could not hold.

    `cost_usd` is None for any group containing a model with no known price —
    a partially-priced total would read as authoritative when it isn't.
    """
    groups: dict[Any, list[dict[str, Any]]] = {}
    for rec in reprice_iter(records, keep_bodies=False):
        groups.setdefault(rec.get(group_by), []).append(rec)

    rows = []
    for key, recs in groups.items():
        costs = [r["cost_usd"] for r in recs]
        # A truncated stream's latency is time-to-disconnect, which is not
        # comparable to time-to-completion — averaging the two together makes
        # both meaningless, so they are counted but not timed.
        timed = [r for r in recs if not r.get("truncated")]
        latencies = [r["latency_ms"] for r in timed if r.get("latency_ms") is not None]
        ttfts = [r["ttft_ms"] for r in timed if r.get("ttft_ms") is not None]
        rows.append(
            {
                group_by: key,
                "calls": len(recs),
                # Failures keep their latency in the percentiles — a 30-second
                # timeout before a 500 is exactly what a latency report is for.
                "errors": sum(1 for r in recs if r.get("status", 200) != 200),
                "truncated": sum(1 for r in recs if r.get("truncated")),
                "input_tokens": sum(r["usage"]["input_tokens"] for r in recs),
                "output_tokens": sum(r["usage"]["output_tokens"] for r in recs),
                "cache_read_tokens": sum(r["usage"]["cache_read_tokens"] for r in recs),
                "cache_write_tokens": sum(r["usage"]["cache_write_tokens"] for r in recs),
                "cost_usd": None if any(c is None for c in costs) else sum(costs),
                "latency_p50_ms": percentile(latencies, 50),
                "latency_p95_ms": percentile(latencies, 95),
                "ttft_p50_ms": percentile(ttfts, 50),
            }
        )
    rows.sort(key=lambda r: (r["cost_usd"] is not None, r["cost_usd"] or 0), reverse=True)
    return rows


# -- live: re-issue and diff -------------------------------------------------


def _provider_for(record: dict[str, Any]) -> str:
    return "anthropic" if record.get("endpoint") == "/v1/messages" else "openai"


def auth_headers(provider: str) -> dict[str, str]:
    if provider == "anthropic":
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set")
        return {
            "x-api-key": key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        }
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise RuntimeError("OPENAI_API_KEY is not set")
    return {"authorization": f"Bearer {key}", "content-type": "application/json"}


def replay_once(
    record: dict[str, Any],
    *,
    model: str | None = None,
    transform: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """Re-issue one logged request and return a fresh record for the result.

    `model` overrides the model; `transform` gets the whole request body and can
    rewrite the prompt. Both leave the log untouched — the baseline stays the
    baseline.
    """
    request = copy.deepcopy(record.get("request") or {})
    if model:
        request["model"] = model
    if transform:
        request = transform(request)
    request.pop("stream", None)

    endpoint = record["endpoint"]
    provider = record.get("provider") or _provider_for(record)
    url = UPSTREAMS[provider] + endpoint

    owned = client is None
    client = client or httpx.Client(timeout=600.0)
    started = time.perf_counter()
    try:
        resp = client.post(url, json=request, headers=auth_headers(provider))
    finally:
        if owned:
            client.close()
    elapsed = (time.perf_counter() - started) * 1000

    try:
        body = resp.json()
    except ValueError:
        body = None
    return build_record(
        endpoint, request, body, status=resp.status_code, latency_ms=elapsed
    )


def replayable(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Records worth re-issuing.

    A baseline that failed has no cost or latency to compare against — pairing
    a zero-cost 429 with a real replay reads as a cost increase that never
    happened. Skip them rather than publish the delta.
    """
    return [r for r in records if r.get("status", 200) == 200]


def replay_live(
    records: list[dict[str, Any]],
    *,
    model: str | None = None,
    transform: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    limit: int | None = None,
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Replay records in order, returning (baseline, replayed) pairs.

    Every credential the run will need is checked before the first request, so
    a missing key for the second provider in a mixed log fails at zero cost
    rather than halfway through a paid run.
    """
    selected = replayable(records)
    if limit:
        selected = selected[:limit]
    selected = reprice(selected)

    for provider in {r.get("provider") or _provider_for(r) for r in selected}:
        auth_headers(provider)

    pairs = []
    with httpx.Client(timeout=600.0) as client:
        for rec in selected:
            pairs.append((rec, replay_once(rec, model=model, transform=transform, client=client)))
    return pairs


def _delta(new: float | None, old: float | None) -> float | None:
    if new is None or old is None:
        return None
    return new - old


def compare(pairs: list[tuple[dict[str, Any], dict[str, Any]]]) -> dict[str, Any]:
    """Diff a replay against its baseline, per request and in total."""
    rows = []
    for base, new in pairs:
        rows.append(
            {
                "id": base.get("id"),
                "prompt_sha": base.get("prompt_sha"),
                "baseline_model": base.get("model"),
                "replay_model": new.get("model"),
                "status": new.get("status"),
                "baseline_cost_usd": base.get("cost_usd"),
                "replay_cost_usd": new.get("cost_usd"),
                "cost_delta_usd": _delta(new.get("cost_usd"), base.get("cost_usd")),
                "baseline_latency_ms": base.get("latency_ms"),
                "replay_latency_ms": new.get("latency_ms"),
                "latency_delta_ms": _delta(new.get("latency_ms"), base.get("latency_ms")),
                "baseline_output_tokens": base["usage"]["output_tokens"],
                "replay_output_tokens": new["usage"]["output_tokens"],
            }
        )

    base_costs = [r["baseline_cost_usd"] for r in rows]
    new_costs = [r["replay_cost_usd"] for r in rows]
    totals = {
        "calls": len(rows),
        "failed": sum(1 for r in rows if r["status"] != 200),
        "baseline_cost_usd": None if any(c is None for c in base_costs) else sum(base_costs),
        "replay_cost_usd": None if any(c is None for c in new_costs) else sum(new_costs),
        "baseline_latency_p50_ms": percentile(
            [r["baseline_latency_ms"] for r in rows if r["baseline_latency_ms"] is not None], 50
        ),
        "replay_latency_p50_ms": percentile(
            [r["replay_latency_ms"] for r in rows if r["replay_latency_ms"] is not None], 50
        ),
    }
    totals["cost_delta_usd"] = _delta(totals["replay_cost_usd"], totals["baseline_cost_usd"])
    return {"rows": rows, "totals": totals}
