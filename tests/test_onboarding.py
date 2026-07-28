"""Getting started should not require reading the source.

The two SDKs disagree about who owns the `/v1` in a base URL, so the same
setting works for one and 404s for the other. These pin the behaviour that
makes that a non-event.
"""

import json

import httpx
import pytest
from starlette.testclient import TestClient

from tollgate.cli import live_line
from tollgate.proxy import ALIASES, create_app

BODY = {
    "model": "claude-opus-5",
    "content": [{"type": "text", "text": "hi"}],
    "usage": {"input_tokens": 100, "output_tokens": 40},
}


class _Response:
    status_code = 200
    headers = {"content-type": "application/json"}

    def __init__(self):
        self.content = json.dumps(BODY).encode()
        self.text = self.content.decode()

    def json(self):
        return BODY


class _Client:
    last_url = None

    def __init__(self, *a, **k):
        pass

    async def post(self, url, content=None, headers=None):
        _Client.last_url = url
        return _Response()

    async def aclose(self):
        pass


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    app = create_app(str(tmp_path / "captured.jsonl"))
    with TestClient(app) as c:
        yield c, app


@pytest.mark.parametrize("alias,canonical", sorted(ALIASES.items()))
def test_a_misconfigured_base_url_still_works(alias, canonical, client):
    """Anthropic appends /v1, OpenAI expects you to supply it. Accept both
    mistakes rather than answering a running proxy with a 404."""
    c, _ = client
    resp = c.post(alias, json={"model": "claude-opus-5", "messages": []})
    assert resp.status_code == 200
    # Whatever shape came in, the upstream sees the canonical path.
    assert _Client.last_url.endswith(canonical)


def test_aliases_are_recorded_under_the_canonical_endpoint(tmp_path, monkeypatch):
    """Otherwise a report splits the same traffic across two rows."""
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    path = tmp_path / "captured.jsonl"
    app = create_app(str(path))
    with TestClient(app) as c:
        c.post("/v1/messages", json={"model": "claude-opus-5", "messages": []})
        c.post("/messages", json={"model": "claude-opus-5", "messages": []})
        c.post("/v1/v1/messages", json={"model": "claude-opus-5", "messages": []})
    app.state.capture_writer.flush()

    endpoints = {json.loads(ln)["endpoint"] for ln in path.read_text().splitlines() if ln}
    assert endpoints == {"/v1/messages"}


def test_the_root_url_confirms_the_proxy_is_alive(client):
    """Somewhere to point a browser when you're not sure it's running."""
    c, _ = client
    body = c.get("/").json()
    assert body["listening"] is True
    assert "/v1/messages" in body["endpoints"]
    assert body["capturing_to"].endswith("captured.jsonl")


def test_an_unknown_path_explains_the_base_url_rather_than_404ing_blankly(client):
    c, _ = client
    resp = c.post("/v0/nonsense", json={})
    assert resp.status_code == 404
    error = resp.json()["error"]
    assert error["type"] == "not_a_tollgate_endpoint"
    # The message has to name the actual fix, not just the failure.
    assert "base_url" in error["message"]
    assert "/v1/messages" in error["endpoints"]
    # The advice has to be pasteable, not a template with {host} in it.
    assert error["anthropic_base_url"].startswith("http")
    assert error["openai_base_url"].endswith("/v1")
    assert "{" not in error["message"]


def test_live_line_shows_what_a_call_cost():
    line = live_line(
        {
            "timestamp": "2026-07-28T15:49:34.123+00:00",
            "model": "claude-opus-5",
            "usage": {"input_tokens": 12403, "output_tokens": 380},
            "latency_ms": 2143.0,
            "cost_usd": 0.0755,
            "status": 200,
        }
    )
    assert "15:49:34" in line
    assert "claude-opus-5" in line
    assert "12,403" in line
    assert "2,143ms" in line
    assert "$0.0755" in line


def test_live_line_flags_failures_and_truncation():
    failed = live_line(
        {"timestamp": "x" * 11 + "00:00:01", "model": "m", "usage": {}, "status": 429}
    )
    assert "[429]" in failed

    cut = live_line(
        {
            "timestamp": "x" * 11 + "00:00:01",
            "model": "m",
            "usage": {},
            "status": 200,
            "truncated": True,
        }
    )
    assert "[truncated]" in cut


def test_live_line_survives_a_sparse_record():
    # An unpriced model or a failed call has None where numbers usually go.
    assert isinstance(live_line({"usage": {}, "model": None, "cost_usd": None}), str)
