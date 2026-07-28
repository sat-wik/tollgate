from tollgate.cache_audit import audit
from tollgate.record import build_record
from tollgate.replay import reprice


def _call(prompt, *, model="claude-opus-5", read=0, write=0, inp=4000, status=200):
    return build_record(
        "/v1/messages",
        {"model": model, "messages": [{"role": "user", "content": prompt}]},
        {
            "model": model,
            "usage": {
                "input_tokens": inp,
                "output_tokens": 100,
                "cache_read_input_tokens": read,
                "cache_creation_input_tokens": write,
            },
        },
        status=status,
    )


def _audit(records):
    return audit(reprice(records))


def test_identical_prompt_never_cached_is_flagged():
    findings = _audit([_call("same big prompt") for _ in range(6)])
    assert [f["finding"] for f in findings] == ["never_cached"]
    finding = findings[0]
    assert finding["calls"] == 6
    assert finding["hit_rate"] == 0.0
    assert finding["recoverable_usd"] > 0
    assert "6 times" in finding["detail"]


def test_a_working_cache_is_not_flagged():
    records = [_call("same big prompt", inp=100, write=4000)]  # first call writes
    records += [_call("same big prompt", inp=100, read=4000) for _ in range(5)]
    assert _audit(records) == []


def test_partial_caching_is_flagged_separately_from_cold():
    # Reads cover a slice of each prompt — a breakpoint placed too early.
    records = [_call("big prompt", inp=3000, read=1000) for _ in range(6)]
    findings = _audit(records)
    assert [f["finding"] for f in findings] == ["partial"]
    assert 0.05 < findings[0]["hit_rate"] < 0.5


def test_changing_prefix_is_flagged_as_invalidation():
    # Every call writes a cache and none reads one, and no two prompts match —
    # the signature of a timestamp or UUID in the prefix.
    records = [_call(f"prompt at 12:0{i}:00", inp=100, write=4000) for i in range(8)]
    findings = _audit(records)
    assert [f["finding"] for f in findings] == ["invalidated"]
    assert findings[0]["calls"] == 8
    assert "change every call" in findings[0]["detail"]
    assert findings[0]["recoverable_usd"] > 0


def test_varied_prompts_that_never_touch_cache_are_not_flagged():
    # No cache writes at all means the app simply isn't using caching here.
    # That may be fine; it is not evidence of invalidation.
    records = [_call(f"unrelated question {i}") for i in range(8)]
    assert _audit(records) == []


def test_short_prompts_are_not_flagged():
    # Below the minimum cacheable prefix, a zero hit rate says nothing.
    records = [_call("hi", inp=50) for _ in range(6)]
    assert _audit(records) == []


def test_a_prompt_seen_twice_is_not_enough_evidence():
    assert _audit([_call("big prompt") for _ in range(2)]) == []


def test_failed_calls_are_excluded_from_the_audit():
    records = [_call("big prompt", status=429) for _ in range(6)]
    assert _audit(records) == []


def test_findings_are_ranked_by_recoverable_spend():
    cheap = [_call("cheap prompt", model="claude-haiku-4-5", inp=1000) for _ in range(5)]
    dear = [_call("costly prompt", model="claude-opus-5", inp=50_000) for _ in range(5)]
    findings = _audit(cheap + dear)
    assert len(findings) == 2
    assert findings[0]["recoverable_usd"] > findings[1]["recoverable_usd"]
    assert findings[0]["model"] == "claude-opus-5"


def test_recoverable_estimate_excludes_the_unavoidable_first_write():
    # 5 calls of 10k tokens on a $5/MTok model, none cached. Four of the five
    # could have been reads at 0.1x, so ~90% of 40k tokens is recoverable.
    findings = _audit([_call("big prompt", inp=10_000) for _ in range(5)])
    assert findings[0]["recoverable_usd"] == 40_000 * 5e-6 * 0.9


def test_unpriced_model_yields_a_finding_without_a_dollar_figure():
    findings = _audit([_call("big prompt", model="mystery-model") for _ in range(5)])
    assert len(findings) == 1
    assert findings[0]["recoverable_usd"] is None
    assert findings[0]["spent_usd"] is None
