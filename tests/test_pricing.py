from datetime import date

import pytest

from tollgate.pricing import (
    cost_usd,
    normalize_usage,
    price_for,
    prompt_tokens,
    rate_card,
    response_model,
    service_tier,
)

TODAY = date(2026, 7, 28)


def usage(**kw):
    base = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "cache_write_5m_tokens": 0,
        "cache_write_1h_tokens": 0,
    }
    base.update(kw)
    return base


# -- model lookup ------------------------------------------------------------


def test_price_lookup_falls_back_to_longest_prefix():
    assert price_for("claude-opus-5", TODAY).input == 5.0
    # A dated snapshot inherits its alias's price.
    assert price_for("claude-haiku-4-5-20251001", TODAY).input == 1.0
    assert price_for("gpt-4o-mini", TODAY).input == 0.15
    assert price_for("gpt-4o", TODAY).input == 2.50


def test_gpt5_point_releases_do_not_collide_on_prefix():
    # "gpt-5" prefixes every one of these; the longest match has to win or
    # gpt-5.6-sol silently prices as the far cheaper gpt-5.
    assert price_for("gpt-5", TODAY).input == 1.25
    assert price_for("gpt-5-mini", TODAY).input == 0.25
    assert price_for("gpt-5.4", TODAY).input == 2.50
    assert price_for("gpt-5.4-mini", TODAY).input == 0.75
    assert price_for("gpt-5.6-sol", TODAY).input == 5.00
    assert price_for("gpt-5.6-sol-2026-07-01", TODAY).input == 5.00


def test_unknown_model_has_no_price_rather_than_zero():
    assert price_for("some-future-model", TODAY) is None
    assert cost_usd("some-future-model", usage(input_tokens=100), at=TODAY) is None


# -- effective-dated rates ---------------------------------------------------


def test_sonnet_5_introductory_rate_expires_on_its_own():
    # Introductory $2/$10 runs through 2026-08-31; standard $3/$15 after.
    assert price_for("claude-sonnet-5", date(2026, 7, 1)).input == 2.0
    assert price_for("claude-sonnet-5", date(2026, 8, 31)).input == 2.0
    assert price_for("claude-sonnet-5", date(2026, 9, 1)).input == 3.0
    assert price_for("claude-sonnet-5", date(2027, 1, 1)).output == 15.0


def test_a_call_is_priced_at_the_rate_in_force_when_it_was_made():
    # The same 1M input tokens, billed either side of the intro expiry.
    tokens = usage(input_tokens=1_000_000)
    assert cost_usd("claude-sonnet-5", tokens, at="2026-08-15T12:00:00+00:00") == 2.0
    assert cost_usd("claude-sonnet-5", tokens, at="2026-09-15T12:00:00+00:00") == 3.0


def test_anthropic_long_context_premium_applies_only_before_it_was_removed():
    # Anthropic charged 2x input / 1.5x output above 200K until 2026-03-13.
    big = usage(input_tokens=300_000, output_tokens=1_000)
    before = cost_usd("claude-sonnet-4-6", big, at=date(2026, 2, 1))
    after = cost_usd("claude-sonnet-4-6", big, at=date(2026, 4, 1))
    assert before == pytest.approx(300_000 * 6e-6 + 1_000 * 22.5e-6)
    assert after == pytest.approx(300_000 * 3e-6 + 1_000 * 15e-6)
    assert before > after


def test_long_context_premium_does_not_apply_below_the_threshold():
    small = usage(input_tokens=100_000, output_tokens=1_000)
    assert cost_usd("claude-sonnet-4-6", small, at=date(2026, 2, 1)) == pytest.approx(
        100_000 * 3e-6 + 1_000 * 15e-6
    )


def test_gpt56_reprices_the_whole_request_above_the_threshold():
    # Above 272K input tokens: 2x input, 1.5x output on the entire request.
    over = usage(input_tokens=300_000, output_tokens=1_000)
    assert cost_usd("gpt-5.6-terra", over, at=TODAY) == pytest.approx(
        300_000 * 5e-6 + 1_000 * 22.5e-6
    )
    under = usage(input_tokens=200_000, output_tokens=1_000)
    assert cost_usd("gpt-5.6-terra", under, at=TODAY) == pytest.approx(
        200_000 * 2.5e-6 + 1_000 * 15e-6
    )


def test_threshold_counts_cached_tokens_as_prompt():
    # A prompt is over the line whether or not the tokens came from cache.
    assert prompt_tokens(usage(input_tokens=10, cache_read_tokens=5, cache_write_tokens=2)) == 17
    mostly_cached = usage(input_tokens=1_000, cache_read_tokens=290_000)
    assert cost_usd("gpt-5.6-luna", mostly_cached, at=TODAY) == pytest.approx(
        1_000 * 2e-6 + 290_000 * 2e-6 * 0.1
    )


def test_undated_call_prices_at_today():
    assert cost_usd("claude-opus-5", usage(input_tokens=1_000_000)) == 5.0


def test_unparseable_timestamp_falls_back_to_today_rather_than_failing():
    assert cost_usd("claude-opus-5", usage(input_tokens=1_000_000), at="not-a-date") == 5.0


# -- usage normalization -----------------------------------------------------


def test_normalize_anthropic_usage_keeps_cache_fields_separate():
    out = normalize_usage(
        {
            "usage": {
                "input_tokens": 100,
                "output_tokens": 50,
                "cache_read_input_tokens": 900,
                "cache_creation_input_tokens": 200,
            }
        }
    )
    assert out["input_tokens"] == 100
    assert out["cache_read_tokens"] == 900
    assert out["cache_write_tokens"] == 200
    # No TTL breakdown reported, so the write is attributed to the cheaper rate.
    assert out["cache_write_5m_tokens"] == 200
    assert out["cache_write_1h_tokens"] == 0


def test_normalize_splits_cache_writes_by_ttl_when_reported():
    out = normalize_usage(
        {
            "usage": {
                "input_tokens": 10,
                "output_tokens": 5,
                "cache_creation_input_tokens": 248,
                "cache_creation": {
                    "ephemeral_5m_input_tokens": 148,
                    "ephemeral_1h_input_tokens": 100,
                },
            }
        }
    )
    assert out["cache_write_5m_tokens"] == 148
    assert out["cache_write_1h_tokens"] == 100
    assert out["cache_write_tokens"] == 248


def test_normalize_openai_chat_usage_subtracts_cached_from_prompt():
    # OpenAI reports prompt_tokens inclusive of the cached portion; Anthropic
    # does not. Normalizing has to undo that so the two are summable.
    out = normalize_usage(
        {
            "usage": {
                "prompt_tokens": 1000,
                "completion_tokens": 20,
                "prompt_tokens_details": {"cached_tokens": 800},
            }
        }
    )
    assert out["input_tokens"] == 200
    assert out["cache_read_tokens"] == 800
    assert out["output_tokens"] == 20


def test_normalize_openai_responses_usage():
    out = normalize_usage(
        {
            "usage": {
                "input_tokens": 500,
                "output_tokens": 30,
                "input_tokens_details": {"cached_tokens": 400},
            }
        }
    )
    assert out["input_tokens"] == 100
    assert out["cache_read_tokens"] == 400


def test_normalize_missing_usage_is_zeros_not_an_error():
    assert normalize_usage(None)["input_tokens"] == 0
    assert normalize_usage({})["output_tokens"] == 0
    assert normalize_usage({"usage": "nonsense"})["cache_read_tokens"] == 0


# -- cost --------------------------------------------------------------------


def test_cache_writes_bill_by_ttl():
    # 1 hour costs 2x input, 5 minutes 1.25x, on a $5/MTok model.
    assert cost_usd(
        "claude-opus-5", usage(cache_write_5m_tokens=1_000_000), at=TODAY
    ) == pytest.approx(6.25)
    assert cost_usd(
        "claude-opus-5", usage(cache_write_1h_tokens=1_000_000), at=TODAY
    ) == pytest.approx(10.0)


def test_legacy_usage_without_a_ttl_split_still_bills_the_write():
    # Logs captured before the split existed carry only cache_write_tokens.
    # Treating that as free would silently under-report every one of them.
    legacy = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 1_000_000,
    }
    assert cost_usd("claude-opus-5", legacy, at=TODAY) == pytest.approx(6.25)


def test_cost_applies_every_multiplier():
    cost = cost_usd(
        "claude-opus-5",
        usage(
            input_tokens=1_000_000,
            output_tokens=1_000_000,
            cache_read_tokens=1_000_000,
            cache_write_5m_tokens=1_000_000,
        ),
        at=TODAY,
    )
    # 5 (input) + 25 (output) + 0.5 (read @ 0.1x) + 6.25 (write @ 1.25x)
    assert cost == pytest.approx(36.75)


def test_openai_cache_discount_is_per_model_not_per_provider():
    # gpt-5 reads cached input at 90% off, gpt-4.1 at 75%, gpt-4o at 50%.
    # A single flat provider-wide multiplier gets two of these three wrong.
    assert price_for("gpt-5.6-luna", TODAY).cache_read_mult == pytest.approx(0.1)
    assert price_for("gpt-4.1", TODAY).cache_read_mult == pytest.approx(0.25)
    assert price_for("gpt-4o", TODAY).cache_read_mult == pytest.approx(0.5)


def test_openai_charges_nothing_to_write_cache():
    assert cost_usd("gpt-4o", usage(cache_write_5m_tokens=1_000_000), at=TODAY) == 0.0


def test_openai_chat_cost_uses_the_uncached_remainder():
    # 10k prompt tokens of which 8k cached, on gpt-4o: 2k @ $2.50 + 8k @ $1.25.
    out = normalize_usage(
        {
            "usage": {
                "prompt_tokens": 10_000,
                "completion_tokens": 0,
                "prompt_tokens_details": {"cached_tokens": 8_000},
            }
        }
    )
    assert cost_usd("gpt-4o", out, at=TODAY) == pytest.approx(
        2_000 * 2.5e-6 + 8_000 * 1.25e-6
    )


# -- service tiers -----------------------------------------------------------


def test_flex_tier_halves_the_bill():
    tokens = usage(input_tokens=1_000_000)
    assert cost_usd("gpt-4o", tokens, at=TODAY, tier="flex") == pytest.approx(1.25)
    assert cost_usd("gpt-4o", tokens, at=TODAY, tier="default") == pytest.approx(2.50)


def test_unmodelled_tier_reports_unknown_rather_than_guessing():
    # Published figures for priority disagree (2x vs 2.5x); a guessed number
    # would be indistinguishable from a real one in a report.
    assert cost_usd("gpt-4o", usage(input_tokens=100), at=TODAY, tier="priority") is None


def test_service_tier_reads_the_response_first():
    # A request asking for "auto" can be served as "flex"; the response wins.
    assert service_tier({"service_tier": "auto"}, {"service_tier": "flex"}) == "flex"
    assert service_tier({"service_tier": "flex"}, {}) == "flex"
    assert service_tier({}, None) is None


# -- misc --------------------------------------------------------------------


def test_served_model_wins_over_requested_model():
    # A server-side fallback bills at whatever model actually answered.
    assert (
        response_model({"model": "claude-fable-5"}, {"model": "claude-opus-4-8"})
        == "claude-opus-4-8"
    )
    assert response_model({"model": "claude-opus-5"}, None) == "claude-opus-5"


def test_rate_card_expands_multipliers_into_dollar_rates():
    card = rate_card("claude-opus-5", TODAY)
    assert card["input"] == 5.0
    assert card["cache_read"] == pytest.approx(0.5)
    assert card["cache_write_5m"] == pytest.approx(6.25)
    assert card["cache_write_1h"] == pytest.approx(10.0)
    assert rate_card("nope", TODAY) is None


def test_pricing_override_file(tmp_path, monkeypatch):
    override = tmp_path / "prices.json"
    override.write_text('{"my-model": {"input": 1.0, "output": 2.0}}')
    monkeypatch.setenv("TOLLGATE_PRICING", str(override))

    import importlib

    from tollgate import pricing

    importlib.reload(pricing)
    try:
        assert pricing.price_for("my-model", TODAY).output == 2.0
    finally:
        monkeypatch.delenv("TOLLGATE_PRICING")
        importlib.reload(pricing)


# -- malformed provider output ------------------------------------------------


def test_float_token_counts_are_not_silently_dropped():
    """A gateway or serializer that round-trips through a float turns 100 into
    100.0. Rejecting that priced a real call at $0 — success-looking failure."""
    out = normalize_usage({"usage": {"input_tokens": 100.0, "output_tokens": 50.0}})
    assert out["input_tokens"] == 100
    assert out["output_tokens"] == 50
    assert cost_usd("claude-opus-5", out, at=TODAY) > 0


def test_string_token_counts_are_coerced():
    out = normalize_usage({"usage": {"input_tokens": "100", "output_tokens": "50"}})
    assert out["input_tokens"] == 100
    assert out["output_tokens"] == 50


def test_negative_token_counts_cannot_produce_a_negative_cost():
    # A negative count is nonsense, and left alone it would offset real spend
    # in whatever group total it lands in.
    out = normalize_usage({"usage": {"input_tokens": -100, "output_tokens": -50}})
    assert out["input_tokens"] == 0
    assert cost_usd("claude-opus-5", out, at=TODAY) == 0.0


def test_a_boolean_is_not_a_token_count():
    # bool is an int subclass in Python, so `True` would otherwise count as 1.
    out = normalize_usage({"usage": {"input_tokens": True, "output_tokens": False}})
    assert out["input_tokens"] == 0
    assert isinstance(out["input_tokens"], int)


def test_non_finite_and_junk_token_counts_are_ignored():
    for junk in (float("nan"), float("inf"), [1], {"a": 1}, None, "abc"):
        out = normalize_usage({"usage": {"input_tokens": junk}})
        assert out["input_tokens"] == 0
        assert cost_usd("claude-opus-5", out, at=TODAY) == 0.0
