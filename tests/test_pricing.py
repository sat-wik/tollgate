import pytest

from tollgate.pricing import cost_usd, normalize_usage, price_for, response_model


def test_price_lookup_falls_back_to_longest_prefix():
    assert price_for("claude-opus-5").input_usd_per_mtok == 5.0
    # A dated snapshot inherits its alias's price.
    assert price_for("claude-haiku-4-5-20251001").input_usd_per_mtok == 1.0
    # The longest matching prefix wins, not the first.
    assert price_for("gpt-4o-mini").input_usd_per_mtok == 0.15
    assert price_for("gpt-4o").input_usd_per_mtok == 2.50


def test_unknown_model_has_no_price_rather_than_zero():
    assert price_for("some-future-model") is None
    assert cost_usd("some-future-model", normalize_usage({"usage": {}})) is None


def test_normalize_anthropic_usage_keeps_cache_fields_separate():
    usage = normalize_usage(
        {
            "usage": {
                "input_tokens": 100,
                "output_tokens": 50,
                "cache_read_input_tokens": 900,
                "cache_creation_input_tokens": 200,
            }
        }
    )
    assert usage == {
        "input_tokens": 100,
        "output_tokens": 50,
        "cache_read_tokens": 900,
        "cache_write_tokens": 200,
    }


def test_normalize_openai_chat_usage_subtracts_cached_from_prompt():
    # OpenAI reports prompt_tokens inclusive of the cached portion; Anthropic
    # does not. Normalizing has to undo that so the two are summable.
    usage = normalize_usage(
        {
            "usage": {
                "prompt_tokens": 1000,
                "completion_tokens": 20,
                "prompt_tokens_details": {"cached_tokens": 800},
            }
        }
    )
    assert usage["input_tokens"] == 200
    assert usage["cache_read_tokens"] == 800
    assert usage["output_tokens"] == 20


def test_normalize_openai_responses_usage():
    usage = normalize_usage(
        {
            "usage": {
                "input_tokens": 500,
                "output_tokens": 30,
                "input_tokens_details": {"cached_tokens": 400},
            }
        }
    )
    assert usage["input_tokens"] == 100
    assert usage["cache_read_tokens"] == 400


def test_normalize_missing_usage_is_zeros_not_an_error():
    assert normalize_usage(None)["input_tokens"] == 0
    assert normalize_usage({})["output_tokens"] == 0
    assert normalize_usage({"usage": "nonsense"})["cache_read_tokens"] == 0


def test_cost_applies_cache_multipliers():
    cost = cost_usd(
        "claude-opus-5",
        {
            "input_tokens": 1_000_000,
            "output_tokens": 1_000_000,
            "cache_read_tokens": 1_000_000,
            "cache_write_tokens": 1_000_000,
        },
    )
    # 5 (input) + 25 (output) + 0.5 (read @ 0.1x) + 6.25 (write @ 1.25x)
    assert cost == pytest.approx(36.75)


def test_served_model_wins_over_requested_model():
    # A server-side fallback bills at whatever model actually answered.
    assert (
        response_model({"model": "claude-fable-5"}, {"model": "claude-opus-4-8"})
        == "claude-opus-4-8"
    )
    assert response_model({"model": "claude-opus-5"}, None) == "claude-opus-5"


def test_pricing_override_file(tmp_path, monkeypatch):
    override = tmp_path / "prices.json"
    override.write_text('{"my-model": {"input": 1.0, "output": 2.0}}')
    monkeypatch.setenv("TOLLGATE_PRICING", str(override))

    import importlib

    from tollgate import pricing

    importlib.reload(pricing)
    try:
        assert pricing.price_for("my-model").output_usd_per_mtok == 2.0
    finally:
        monkeypatch.delenv("TOLLGATE_PRICING")
        importlib.reload(pricing)


def test_openai_cache_discount_is_per_model_not_per_provider():
    # gpt-5 reads cached input at 90% off, gpt-4.1 at 75%, gpt-4o at 50%.
    # A single flat provider-wide multiplier gets two of these three wrong.
    assert price_for("gpt-5.6-luna").cache_read_mult == pytest.approx(0.1)
    assert price_for("gpt-4.1").cache_read_mult == pytest.approx(0.25)
    assert price_for("gpt-4o").cache_read_mult == pytest.approx(0.5)


def test_openai_charges_nothing_to_write_cache():
    usage = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 1_000_000,
    }
    assert cost_usd("gpt-4o", usage) == 0.0
    # Anthropic does bill the write, at 1.25x input for a 5-minute TTL.
    assert cost_usd("claude-opus-5", usage) == pytest.approx(6.25)


def test_gpt5_point_releases_do_not_collide_on_prefix():
    # "gpt-5" prefixes every one of these; the longest match has to win or
    # gpt-5.6-sol silently prices as the far cheaper gpt-5.
    assert price_for("gpt-5").input_usd_per_mtok == 1.25
    assert price_for("gpt-5-mini").input_usd_per_mtok == 0.25
    assert price_for("gpt-5.4").input_usd_per_mtok == 2.50
    assert price_for("gpt-5.4-mini").input_usd_per_mtok == 0.75
    assert price_for("gpt-5.6-sol").input_usd_per_mtok == 5.00
    assert price_for("gpt-5.6-sol-2026-07-01").input_usd_per_mtok == 5.00


def test_openai_chat_cost_uses_the_uncached_remainder():
    # 10k prompt tokens of which 8k cached, on gpt-4o: 2k @ $2.50 + 8k @ $1.25.
    usage = normalize_usage(
        {
            "usage": {
                "prompt_tokens": 10_000,
                "completion_tokens": 0,
                "prompt_tokens_details": {"cached_tokens": 8_000},
            }
        }
    )
    assert cost_usd("gpt-4o", usage) == pytest.approx(2_000 * 2.5e-6 + 8_000 * 1.25e-6)
