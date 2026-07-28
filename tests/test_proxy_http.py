"""HTTP-level tests: the proxy is exercised through its actual routes.

The upstream is stubbed by swapping httpx.AsyncClient, so these stay offline
while still covering the FastAPI routing layer — which is where an unannotated
handler parameter silently turns every call into a 422.
"""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from tollgate.proxy import create_app

ANTHROPIC_BODY = {
    "model": "claude-opus-5",
    "content": [{"type": "text", "text": "hi"}],
    "usage": {"input_tokens": 100, "output_tokens": 40},
}

SSE_CHUNKS = [
    b'data: {"type":"message_start","message":{"model":"claude-opus-5","usage":{"input_tokens":100}}}\n\n',
    b'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"hi"}}\n\n',
    b'data: {"type":"message_delta","usage":{"output_tokens":40}}\n\n',
]


class _StubResponse:
    def __init__(self, *, json_body=None, chunks=None, status_code=200, content_type):
        self._json = json_body
        self._chunks = chunks or []
        self.status_code = status_code
        self.headers = {"content-type": content_type}
        self.content = json.dumps(json_body).encode() if json_body is not None else b""
        self.text = self.content.decode()

    def json(self):
        if self._json is None:
            raise ValueError("not json")
        return self._json

    async def aiter_bytes(self):
        for chunk in self._chunks:
            yield chunk


class _StubStream:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, *exc):
        return False


class _StubClient:
    """Stands in for httpx.AsyncClient, recording what the proxy forwarded."""

    captured: dict = {}
    instances: int = 0

    def __init__(self, *args, **kwargs):
        type(self).instances += 1

    async def post(self, url, content=None, headers=None):
        _StubClient.captured = {"url": url, "content": content, "headers": headers}
        return _StubResponse(json_body=ANTHROPIC_BODY, content_type="application/json")

    def stream(self, method, url, content=None, headers=None):
        _StubClient.captured = {"url": url, "content": content, "headers": headers}
        return _StubStream(
            _StubResponse(chunks=SSE_CHUNKS, content_type="text/event-stream")
        )

    async def aclose(self):
        pass


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(httpx, "AsyncClient", _StubClient)
    path = tmp_path / "captured.jsonl"
    with TestClient(create_app(str(path))) as c:
        yield c, path


def _only_record(path):
    lines = path.read_text().strip().splitlines()
    assert len(lines) == 1, lines
    return json.loads(lines[0])


def test_non_streaming_call_is_proxied_and_captured(client):
    c, path = client
    resp = c.post(
        "/v1/messages",
        json={"model": "claude-opus-5", "messages": [{"role": "user", "content": "hi"}]},
        headers={"x-api-key": "sk-secret"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == ANTHROPIC_BODY

    record = _only_record(path)
    assert record["model"] == "claude-opus-5"
    assert record["stream"] is False
    assert record["usage"]["output_tokens"] == 40
    assert record["cost_usd"] == pytest.approx(100 * 5e-6 + 40 * 25e-6)
    assert record["latency_ms"] is not None


def test_streaming_call_relays_chunks_and_reconstructs_the_response(client):
    c, path = client
    with c.stream(
        "POST",
        "/v1/messages",
        json={
            "model": "claude-opus-5",
            "stream": True,
            "messages": [{"role": "user", "content": "hi"}],
        },
        headers={"x-api-key": "sk-secret"},
    ) as resp:
        assert resp.status_code == 200
        body = b"".join(resp.iter_bytes())
    assert body == b"".join(SSE_CHUNKS)

    record = _only_record(path)
    assert record["stream"] is True
    assert record["response"]["content"][0]["text"] == "hi"
    assert record["usage"] == {
        "input_tokens": 100,
        "output_tokens": 40,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
    }
    assert record["ttft_ms"] is not None


def test_auth_headers_are_forwarded_upstream_but_never_logged(client):
    c, path = client
    c.post(
        "/v1/messages",
        json={"model": "claude-opus-5", "messages": [{"role": "user", "content": "hi"}]},
        headers={"x-api-key": "sk-secret"},
    )
    forwarded = {k.lower(): v for k, v in _StubClient.captured["headers"].items()}
    assert forwarded["x-api-key"] == "sk-secret"
    assert "sk-secret" not in path.read_text()


def test_openai_endpoints_route_to_the_openai_upstream(client):
    c, _ = client
    c.post("/v1/chat/completions", json={"model": "gpt-4o", "messages": []})
    assert _StubClient.captured["url"].endswith("/v1/chat/completions")
    assert "openai" in _StubClient.captured["url"]


def test_non_200_upstream_is_relayed_and_captured_as_a_free_failure(tmp_path, monkeypatch):
    class _Failing(_StubClient):
        async def post(self, url, content=None, headers=None):
            return _StubResponse(
                json_body={"error": {"type": "rate_limit_error"}},
                status_code=429,
                content_type="application/json",
            )

    monkeypatch.setattr(httpx, "AsyncClient", _Failing)
    path = tmp_path / "captured.jsonl"
    with TestClient(create_app(str(path))) as c:
        resp = c.post(
            "/v1/messages", json={"model": "claude-opus-5", "messages": []}
        )
    assert resp.status_code == 429

    record = _only_record(path)
    assert record["status"] == 429
    assert record["response"]["error"]["type"] == "rate_limit_error"
    # A rejected request is not billed, but its latency is still real.
    assert record["cost_usd"] == 0.0
    assert record["latency_ms"] is not None


def test_upstream_error_with_a_non_json_body_is_still_captured(tmp_path, monkeypatch):
    class _Failing(_StubClient):
        async def post(self, url, content=None, headers=None):
            resp = _StubResponse(status_code=502, content_type="text/html")
            resp.content = b"<html>502 Bad Gateway</html>"
            resp.text = "<html>502 Bad Gateway</html>"
            resp._json = None
            return resp

    monkeypatch.setattr(httpx, "AsyncClient", _Failing)
    path = tmp_path / "captured.jsonl"
    with TestClient(create_app(str(path))) as c:
        resp = c.post("/v1/messages", json={"model": "claude-opus-5", "messages": []})
    assert resp.status_code == 502

    record = _only_record(path)
    assert record["status"] == 502
    assert "502 Bad Gateway" in record["response"]["error"]["message"]


def test_upstream_client_is_pooled_across_requests(tmp_path, monkeypatch):
    """A client per request would put a TLS handshake inside every measurement."""
    monkeypatch.setattr(httpx, "AsyncClient", _StubClient)
    _StubClient.instances = 0
    path = tmp_path / "captured.jsonl"
    with TestClient(create_app(str(path))) as c:
        for _ in range(3):
            c.post(
                "/v1/messages",
                json={"model": "claude-opus-5", "messages": []},
            )
    assert _StubClient.instances == 1
