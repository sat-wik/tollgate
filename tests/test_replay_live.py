"""Live replay, with the provider stubbed out.

`replay_live` spends real money, so it must be exercised offline — and it needs
to behave sanely on the paths that only show up mid-run: a missing key, an
upstream that starts failing, a baseline that was itself a failure.
"""

import json

import httpx
import pytest

from tollgate.record import build_record
from tollgate.replay import auth_headers, compare, replay_live, replay_once


def _record(model="claude-opus-5", in_tok=1_000_000, out_tok=0, latency=400.0, status=200):
    return build_record(
        "/v1/messages",
        {
            "model": model,
            "max_tokens": 100,
            "stream": True,
            "messages": [{"role": "user", "content": "hi"}],
        },
        {
            "model": model,
            "content": [{"type": "text", "text": "ok"}],
            "usage": {"input_tokens": in_tok, "output_tokens": out_tok},
        },
        status=status,
        latency_ms=latency,
    )


class _StubClient:
    """Records every outbound request and replies from a scripted queue."""

    def __init__(self, replies=None):
        self.sent = []
        self.replies = list(replies or [])

    def post(self, url, json=None, headers=None):
        self.sent.append({"url": url, "body": json, "headers": headers})
        if self.replies:
            return self.replies.pop(0)
        return _StubResponse()

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _StubResponse:
    def __init__(self, body=None, status_code=200):
        self._body = body if body is not None else {
            "model": "claude-haiku-4-5",
            "usage": {"input_tokens": 1_000_000, "output_tokens": 0},
        }
        self.status_code = status_code

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body


@pytest.fixture(autouse=True)
def _keys(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")


def test_replay_strips_stream_so_both_sides_are_one_body():
    client = _StubClient()
    replay_once(_record(), client=client)
    assert "stream" not in client.sent[0]["body"]


def test_replay_applies_the_model_override_without_touching_the_baseline():
    baseline = _record()
    client = _StubClient()
    replay_once(baseline, model="claude-haiku-4-5", client=client)
    assert client.sent[0]["body"]["model"] == "claude-haiku-4-5"
    # The logged record is the baseline and must survive intact.
    assert baseline["request"]["model"] == "claude-opus-5"
    assert baseline["request"]["stream"] is True


def test_replay_applies_a_prompt_transform():
    client = _StubClient()
    replay_once(
        _record(),
        transform=lambda req: {**req, "system": "be terse"},
        client=client,
    )
    assert client.sent[0]["body"]["system"] == "be terse"


def test_replay_sends_provider_credentials():
    client = _StubClient()
    replay_once(_record(), client=client)
    headers = client.sent[0]["headers"]
    assert headers["x-api-key"] == "sk-test"
    assert "anthropic-version" in headers


def test_openai_records_route_to_openai_with_bearer_auth():
    record = build_record(
        "/v1/chat/completions",
        {"model": "gpt-4o", "messages": []},
        {"model": "gpt-4o", "usage": {"prompt_tokens": 10, "completion_tokens": 1}},
    )
    client = _StubClient()
    replay_once(record, client=client)
    assert "openai" in client.sent[0]["url"]
    assert client.sent[0]["headers"]["authorization"] == "Bearer sk-test"


def test_missing_key_is_caught_before_any_request_is_paid_for(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        replay_live([_record(), _record()])


def test_replay_live_reuses_one_client_across_records(monkeypatch):
    client = _StubClient()
    monkeypatch.setattr(httpx, "Client", lambda **kw: client)
    replay_live([_record(), _record(), _record()])
    assert len(client.sent) == 3


def test_limit_stops_before_spending_on_the_rest(monkeypatch):
    client = _StubClient()
    monkeypatch.setattr(httpx, "Client", lambda **kw: client)
    pairs = replay_live([_record() for _ in range(10)], limit=2)
    assert len(pairs) == 2
    assert len(client.sent) == 2


def test_failed_baselines_are_skipped(monkeypatch):
    # Re-issuing a call that was rate-limited yields a meaningless delta: a
    # zero-cost baseline against a real replay cost.
    client = _StubClient()
    monkeypatch.setattr(httpx, "Client", lambda **kw: client)
    pairs = replay_live([_record(status=429), _record(), _record(status=500)])
    assert len(pairs) == 1
    assert len(client.sent) == 1


def test_an_upstream_failure_during_replay_is_reported_not_raised(monkeypatch):
    client = _StubClient(
        replies=[
            _StubResponse(body={"error": {"type": "overloaded_error"}}, status_code=529),
            _StubResponse(),
        ]
    )
    monkeypatch.setattr(httpx, "Client", lambda **kw: client)
    result = compare(replay_live([_record(), _record()]))
    assert result["totals"]["calls"] == 2
    assert result["totals"]["failed"] == 1
    # The failed replay is free, so the delta is a pure saving against baseline.
    assert result["rows"][0]["replay_cost_usd"] == 0.0


def test_a_non_json_upstream_response_does_not_crash_the_run(monkeypatch):
    client = _StubClient(replies=[_StubResponse(body=None, status_code=502)])
    monkeypatch.setattr(httpx, "Client", lambda **kw: client)
    result = compare(replay_live([_record()]))
    assert result["rows"][0]["status"] == 502


def test_compare_totals_a_model_swap(monkeypatch):
    client = _StubClient()
    monkeypatch.setattr(httpx, "Client", lambda **kw: client)
    result = compare(replay_live([_record()], model="claude-haiku-4-5"))
    row = result["rows"][0]
    assert row["baseline_model"] == "claude-opus-5"
    assert row["replay_model"] == "claude-haiku-4-5"
    # $5.00/MTok down to $1.00/MTok on a million input tokens.
    assert row["cost_delta_usd"] == pytest.approx(-4.0)
    assert result["totals"]["cost_delta_usd"] == pytest.approx(-4.0)


def test_auth_headers_names_the_variable_it_wants():
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        import os

        key = os.environ.pop("OPENAI_API_KEY")
        try:
            auth_headers("openai")
        finally:
            os.environ["OPENAI_API_KEY"] = key


def test_replayed_record_is_serializable(monkeypatch):
    client = _StubClient()
    monkeypatch.setattr(httpx, "Client", lambda **kw: client)
    _, replayed = replay_live([_record()])[0]
    json.dumps(replayed)


def test_a_missing_key_for_the_second_provider_fails_before_spending(monkeypatch):
    """The hazard the single-provider case hides: an Anthropic-first log with
    no OpenAI key would otherwise pay for record one, then die on record two."""
    client = _StubClient()
    monkeypatch.setattr(httpx, "Client", lambda **kw: client)
    monkeypatch.delenv("OPENAI_API_KEY")
    mixed = [
        _record(),
        build_record(
            "/v1/chat/completions",
            {"model": "gpt-4o", "messages": []},
            {"model": "gpt-4o", "usage": {"prompt_tokens": 10, "completion_tokens": 1}},
        ),
    ]
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        replay_live(mixed)
    assert client.sent == []
