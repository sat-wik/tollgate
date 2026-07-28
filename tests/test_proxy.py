import json

from tollgate.proxy import (
    accumulate_anthropic,
    accumulate_openai_chat,
    accumulate_openai_responses,
    create_app,
)
from tollgate.record import build_record, log_record


def test_accumulate_openai_chat_stream():
    raw = "\n".join(
        [
            'data: {"model": "gpt-4o", "choices": [{"delta": {"role": "assistant"}}]}',
            'data: {"model": "gpt-4o", "choices": [{"delta": {"content": "Hel"}}]}',
            'data: {"model": "gpt-4o", "choices": [{"delta": {"content": "lo"}}]}',
            'data: {"model": "gpt-4o", "choices": [], "usage": {"prompt_tokens": 9, "completion_tokens": 2}}',
            "data: [DONE]",
        ]
    )
    out = accumulate_openai_chat(raw)
    assert out["choices"][0]["message"]["content"] == "Hello"
    assert out["model"] == "gpt-4o"
    assert out["usage"]["prompt_tokens"] == 9


def test_accumulate_anthropic_stream():
    raw = "\n".join(
        [
            'data: {"type": "message_start", "message": {"model": "claude-opus-4-8", "usage": {"input_tokens": 12}}}',
            'data: {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Bonj"}}',
            'data: {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "our"}}',
            'data: {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 4}}',
        ]
    )
    out = accumulate_anthropic(raw)
    assert out["content"][0]["text"] == "Bonjour"
    assert out["model"] == "claude-opus-4-8"
    assert out["usage"] == {"input_tokens": 12, "output_tokens": 4}


def test_accumulate_responses_stream():
    raw = "\n".join(
        [
            'data: {"type": "response.output_text.delta", "delta": "par"}',
            'data: {"type": "response.output_text.delta", "delta": "tial"}',
            'data: {"type": "response.completed", "response": {"output_text": "full", "usage": {"input_tokens": 5}}}',
        ]
    )
    assert accumulate_openai_responses(raw)["output_text"] == "full"
    only_deltas = 'data: {"type": "response.output_text.delta", "delta": "x"}'
    assert accumulate_openai_responses(only_deltas)["output_text"] == "x"


def test_log_record_roundtrip(tmp_path):
    path = tmp_path / "captured.jsonl"
    log_record(
        str(path),
        build_record(
            "/v1/messages",
            {"model": "claude-opus-4-8", "messages": [{"role": "user", "content": "hi"}]},
            {
                "model": "claude-opus-4-8",
                "content": [{"type": "text", "text": "hello"}],
                "usage": {"input_tokens": 10, "output_tokens": 5},
            },
            latency_ms=812.35,
        ),
    )
    entry = json.loads(path.read_text().strip())
    assert entry["endpoint"] == "/v1/messages"
    assert entry["provider"] == "anthropic"
    assert entry["request"]["model"] == "claude-opus-4-8"
    assert entry["response"]["content"][0]["text"] == "hello"
    assert entry["latency_ms"] == 812.4
    assert entry["usage"]["input_tokens"] == 10
    # 10 in @ $5/Mtok + 5 out @ $25/Mtok
    assert entry["cost_usd"] == 10 * 5e-6 + 5 * 25e-6
    assert "timestamp" in entry


def test_app_exposes_all_three_endpoints(tmp_path):
    app = create_app(str(tmp_path / "c.jsonl"))
    routes = {r.path for r in app.routes}
    assert {"/v1/chat/completions", "/v1/messages", "/v1/responses"} <= routes
