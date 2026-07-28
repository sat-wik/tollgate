import json

import pytest

from tollgate.record import build_record, load_records, log_record, prompt_sha, request_sha
from tollgate.replay import compare, percentile, reprice, summarize


def _record(model, in_tok, out_tok, latency, prompt="hi"):
    return build_record(
        "/v1/messages",
        {"model": model, "messages": [{"role": "user", "content": prompt}]},
        {
            "model": model,
            "content": [{"type": "text", "text": "ok"}],
            "usage": {"input_tokens": in_tok, "output_tokens": out_tok},
        },
        latency_ms=latency,
    )


def test_prompt_sha_ignores_model_but_request_sha_does_not():
    a = {"model": "claude-opus-5", "messages": [{"role": "user", "content": "hi"}]}
    b = {"model": "claude-sonnet-5", "messages": [{"role": "user", "content": "hi"}]}
    assert prompt_sha(a) == prompt_sha(b)
    assert request_sha(a) != request_sha(b)


def test_prompt_sha_is_stable_under_key_order():
    a = {"model": "m", "system": "s", "messages": [{"role": "user", "content": "hi"}]}
    b = {"messages": [{"content": "hi", "role": "user"}], "system": "s", "model": "m"}
    assert prompt_sha(a) == prompt_sha(b)


def test_prompt_sha_changes_when_the_prompt_does():
    a = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
    b = {"model": "m", "messages": [{"role": "user", "content": "hello"}]}
    assert prompt_sha(a) != prompt_sha(b)


def test_reprice_is_deterministic_and_recomputes_from_the_raw_pair():
    rec = _record("claude-opus-5", 1_000_000, 0, 100.0)
    # A stale/wrong cost on the stored record is corrected from the raw pair.
    rec["cost_usd"] = 999.0
    rec["usage"] = {}
    first = reprice([rec])
    second = reprice([rec])
    assert first == second
    assert first[0]["cost_usd"] == 5.0
    assert first[0]["usage"]["input_tokens"] == 1_000_000


def test_summarize_groups_and_sorts_by_cost():
    records = [
        _record("claude-opus-5", 1_000_000, 0, 300.0),
        _record("claude-haiku-4-5", 1_000_000, 0, 100.0),
        _record("claude-haiku-4-5", 1_000_000, 0, 200.0),
    ]
    rows = summarize(records)
    assert [r["model"] for r in rows] == ["claude-opus-5", "claude-haiku-4-5"]
    assert rows[0]["cost_usd"] == 5.0
    assert rows[1]["calls"] == 2
    assert rows[1]["cost_usd"] == 2.0
    assert rows[1]["latency_p50_ms"] == 100.0


def test_summarize_by_prompt_sha_groups_across_models():
    records = [
        _record("claude-opus-5", 10, 10, 100.0, prompt="same"),
        _record("claude-sonnet-5", 10, 10, 100.0, prompt="same"),
    ]
    assert len(summarize(records, group_by="prompt_sha")) == 1


def test_summarize_withholds_a_total_it_cannot_fully_price():
    records = [_record("claude-opus-5", 10, 10, 100.0), _record("mystery-model", 10, 10, 100.0)]
    rows = summarize(records)
    mystery = next(r for r in rows if r["model"] == "mystery-model")
    assert mystery["cost_usd"] is None
    assert mystery["calls"] == 1


def test_percentile_nearest_rank():
    assert percentile([], 50) is None
    assert percentile([10.0], 95) == 10.0
    assert percentile([1.0, 2.0, 3.0, 4.0], 50) == 2.0
    assert percentile([1.0, 2.0, 3.0, 4.0], 95) == 4.0


def test_compare_reports_per_call_and_total_deltas():
    baseline = reprice([_record("claude-opus-5", 1_000_000, 0, 400.0)])[0]
    replayed = reprice([_record("claude-haiku-4-5", 1_000_000, 0, 150.0)])[0]
    result = compare([(baseline, replayed)])

    row = result["rows"][0]
    assert row["baseline_model"] == "claude-opus-5"
    assert row["replay_model"] == "claude-haiku-4-5"
    assert row["cost_delta_usd"] == -4.0
    assert row["latency_delta_ms"] == -250.0

    totals = result["totals"]
    assert totals["calls"] == 1
    assert totals["failed"] == 0
    assert totals["cost_delta_usd"] == -4.0


def test_compare_leaves_deltas_none_when_a_side_is_unpriced():
    baseline = reprice([_record("mystery-model", 10, 10, 100.0)])[0]
    replayed = reprice([_record("claude-opus-5", 10, 10, 100.0)])[0]
    result = compare([(baseline, replayed)])
    assert result["rows"][0]["cost_delta_usd"] is None
    assert result["totals"]["cost_delta_usd"] is None


def test_load_records_skips_a_torn_trailing_line(tmp_path):
    path = tmp_path / "captured.jsonl"
    log_record(str(path), _record("claude-opus-5", 10, 10, 100.0))
    with open(path, "a") as f:
        f.write('{"partial": tr')  # proxy was killed mid-append
    records = load_records(str(path))
    assert len(records) == 1
    assert records[0]["model"] == "claude-opus-5"


def test_record_is_json_serializable_end_to_end(tmp_path):
    path = tmp_path / "captured.jsonl"
    log_record(str(path), _record("claude-opus-5", 10, 10, 100.0))
    assert json.loads(path.read_text().strip())["cost_usd"] is not None


def test_v020_logs_reprice_correctly_at_their_historical_rate(tmp_path):
    """An older log lacks the TTL split and carries a cost computed at the
    wrong rate. Repricing from the raw pair has to fix both."""
    path = tmp_path / "old.jsonl"
    legacy = {
        "id": "abc123",
        "timestamp": "2026-02-10T10:00:00.000+00:00",
        "endpoint": "/v1/messages",
        "provider": "anthropic",
        "model": "claude-sonnet-4-6",
        "status": 200,
        "latency_ms": 900.0,
        # No service_tier key, and no cache_write_5m/1h split.
        "usage": {
            "input_tokens": 250_000,
            "output_tokens": 1_000,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
        },
        "cost_usd": 0.765,  # computed with a flat rate that didn't apply then
        "prompt_sha": "deadbeefdeadbeef",
        "request": {"model": "claude-sonnet-4-6", "messages": []},
        "response": {
            "model": "claude-sonnet-4-6",
            "usage": {"input_tokens": 250_000, "output_tokens": 1_000},
        },
    }
    path.write_text(json.dumps(legacy) + "\n")

    repriced = reprice(load_records(str(path)))[0]
    # February 2026 predates flat long-context pricing, so a 250K-token prompt
    # billed at 2x input / 1.5x output.
    assert repriced["cost_usd"] == pytest.approx(250_000 * 6e-6 + 1_000 * 22.5e-6)
    assert repriced["cost_usd"] > legacy["cost_usd"]
    assert repriced["usage"]["cache_write_5m_tokens"] == 0


def test_truncated_calls_are_counted_but_kept_out_of_the_percentiles():
    """A cut-short stream's latency is time-to-disconnect. Averaging it with
    completion latency makes both numbers meaningless."""
    whole = [_record("claude-opus-5", 10, 10, 3000.0) for _ in range(3)]
    cut = _record("claude-opus-5", 10, 0, 40.0)
    cut["truncated"] = True

    row = summarize(whole + [cut])[0]
    assert row["calls"] == 4
    assert row["truncated"] == 1
    # 40ms would otherwise drag p50 down by a factor of ~75.
    assert row["latency_p50_ms"] == 3000.0


def test_records_without_a_truncated_field_are_treated_as_complete():
    row = summarize([_record("claude-opus-5", 10, 10, 100.0)])[0]
    assert row["truncated"] == 0
    assert row["latency_p50_ms"] == 100.0


def test_reprice_iter_can_drop_bodies_without_changing_the_numbers():
    from tollgate.replay import reprice_iter

    records = [_record("claude-opus-5", 1_000_000, 0, 100.0) for _ in range(3)]
    fat = list(reprice_iter(records, keep_bodies=True))
    slim = list(reprice_iter(records, keep_bodies=False))
    assert [r["cost_usd"] for r in fat] == [r["cost_usd"] for r in slim]
    assert "request" in fat[0] and "request" not in slim[0]


def test_summarize_accepts_an_iterator(tmp_path):
    from tollgate.record import iter_records

    path = tmp_path / "c.jsonl"
    for _ in range(3):
        log_record(str(path), _record("claude-opus-5", 1_000_000, 0, 100.0))
    streamed = summarize(iter_records(str(path)))
    buffered = summarize(load_records(str(path)))
    assert streamed == buffered
    assert streamed[0]["cost_usd"] == 15.0
