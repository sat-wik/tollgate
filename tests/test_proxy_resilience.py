"""Tollgate sits in someone else's request path. It must not break it.

An observe-only proxy has a hard obligation: any failure of its own —
unwritable capture file, missing lifespan, unreachable upstream — must degrade
observation, never the request being observed.
"""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from tollgate import proxy
from tollgate.proxy import create_app

BODY = {
    "model": "claude-opus-5",
    "content": [{"type": "text", "text": "hi"}],
    "usage": {"input_tokens": 100, "output_tokens": 40},
}

REQUEST = {"model": "claude-opus-5", "messages": [{"role": "user", "content": "hi"}]}


class _Response:
    status_code = 200
    headers = {"content-type": "application/json"}

    def __init__(self):
        self.content = json.dumps(BODY).encode()
        self.text = self.content.decode()

    def json(self):
        return BODY


class _Client:
    def __init__(self, *args, **kwargs):
        pass

    async def post(self, url, content=None, headers=None):
        return _Response()

    def stream(self, method, url, content=None, headers=None):
        raise AssertionError("not used")

    async def aclose(self):
        pass


@pytest.fixture(autouse=True)
def _reset_warning():
    proxy._capture_warned = False
    yield
    proxy._capture_warned = False


def test_an_unwritable_capture_file_does_not_fail_the_request(tmp_path, monkeypatch, capsys):
    """The provider answered and the caller was billed. Losing the log entry is
    bad; turning that into a 500 for the caller is worse."""
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    blocked = tmp_path / "readonly"
    blocked.mkdir()
    blocked.chmod(0o500)
    try:
        with TestClient(create_app(str(blocked / "captured.jsonl"))) as c:
            resp = c.post("/v1/messages", json=REQUEST)
        assert resp.status_code == 200
        assert resp.json() == BODY
        assert "capture failed" in capsys.readouterr().err
    finally:
        blocked.chmod(0o700)


def test_the_capture_failure_is_reported_once_not_per_request(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    blocked = tmp_path / "readonly"
    blocked.mkdir()
    blocked.chmod(0o500)
    try:
        with TestClient(create_app(str(blocked / "captured.jsonl"))) as c:
            for _ in range(5):
                assert c.post("/v1/messages", json=REQUEST).status_code == 200
        # A misconfigured path should be visible without drowning the log of
        # the application being observed.
        assert capsys.readouterr().err.count("capture failed") == 1
    finally:
        blocked.chmod(0o700)


def test_an_unreachable_upstream_is_recorded_and_relayed_as_502(tmp_path, monkeypatch):
    class _Unreachable(_Client):
        async def post(self, url, content=None, headers=None):
            raise httpx.ConnectError("nodename nor servname provided")

    monkeypatch.setattr(httpx, "AsyncClient", _Unreachable)
    path = tmp_path / "captured.jsonl"
    with TestClient(create_app(str(path))) as c:
        resp = c.post("/v1/messages", json=REQUEST)

    assert resp.status_code == 502
    assert resp.json()["error"]["type"] == "upstream_connection_error"

    # A provider you could not reach is exactly what a reliability log is for.
    record = json.loads(path.read_text().strip())
    assert record["status"] == 502
    assert record["cost_usd"] == 0.0
    assert record["latency_ms"] is not None
    assert "ConnectError" in record["response"]["error"]["message"]


def test_an_unreachable_upstream_on_a_streaming_call_is_also_recorded(tmp_path, monkeypatch):
    class _Unreachable(_Client):
        def stream(self, method, url, content=None, headers=None):
            class _Ctx:
                async def __aenter__(self):
                    raise httpx.ConnectTimeout("timed out")

                async def __aexit__(self, *exc):
                    return False

            return _Ctx()

    monkeypatch.setattr(httpx, "AsyncClient", _Unreachable)
    path = tmp_path / "captured.jsonl"
    with TestClient(create_app(str(path))) as c:
        resp = c.post("/v1/messages", json={**REQUEST, "stream": True})

    assert resp.status_code == 502
    record = json.loads(path.read_text().strip())
    assert record["status"] == 502
    assert record["stream"] is True


def test_the_app_works_when_its_lifespan_never_ran(tmp_path, monkeypatch):
    """Sub-mounted and embedded apps don't always get their lifespan run, and a
    KeyError at request time is a poor way to discover that."""
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    path = tmp_path / "captured.jsonl"
    client = TestClient(create_app(str(path)))  # no context manager, no startup
    resp = client.post("/v1/messages", json=REQUEST)
    assert resp.status_code == 200
    assert json.loads(path.read_text().strip())["model"] == "claude-opus-5"
