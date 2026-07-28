"""Client disconnect mid-stream, over real HTTP.

This one needs a live server: Starlette's TestClient buffers the whole response
before handing back bytes, so abandoning its iterator never reaches the relay
generator and the disconnect path would silently go untested.
"""

import asyncio
import json
import socket
import threading
import time

import httpx
import pytest
import uvicorn
from starlette.applications import Starlette
from starlette.responses import StreamingResponse
from starlette.routing import Route

CHUNK = b'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"tok"}}\n\n'


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _serve(app, port):
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    )
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                return server
        except OSError:
            time.sleep(0.05)
    raise RuntimeError("server did not start")


@pytest.fixture(scope="module")
def proxy_url(tmp_path_factory):
    async def messages(request):
        async def gen():
            yield (
                b'data: {"type":"message_start","message":'
                b'{"model":"claude-opus-5","usage":{"input_tokens":100}}}\n\n'
            )
            for _ in range(12):
                await asyncio.sleep(0.05)
                yield CHUNK
            yield b'data: {"type":"message_delta","usage":{"output_tokens":500}}\n\n'

        return StreamingResponse(gen(), media_type="text/event-stream")

    upstream = Starlette(routes=[Route("/v1/messages", messages, methods=["POST"])])
    up_port = _free_port()
    _serve(upstream, up_port)

    import importlib
    import os

    os.environ["TOLLGATE_UPSTREAM_ANTHROPIC"] = f"http://127.0.0.1:{up_port}"
    from tollgate import proxy

    importlib.reload(proxy)

    log = tmp_path_factory.mktemp("capture") / "captured.jsonl"
    port = _free_port()
    _serve(proxy.create_app(str(log)), port)
    yield f"http://127.0.0.1:{port}", log

    os.environ.pop("TOLLGATE_UPSTREAM_ANTHROPIC", None)
    importlib.reload(proxy)


BODY = {"model": "claude-opus-5", "stream": True, "messages": [{"role": "user", "content": "hi"}]}


def _records(log):
    return [json.loads(ln) for ln in log.read_text().splitlines() if ln.strip()]


def test_disconnect_is_recorded_as_truncated_and_completion_is_not(proxy_url):
    url, log = proxy_url

    with httpx.stream("POST", f"{url}/v1/messages", json=BODY, timeout=30) as s:
        for i, _ in enumerate(s.iter_bytes()):
            if i >= 2:
                break  # hang up mid-stream
    time.sleep(1.0)

    with httpx.stream("POST", f"{url}/v1/messages", json=BODY, timeout=30) as s:
        list(s.iter_bytes())
    time.sleep(0.5)

    records = _records(log)
    assert len(records) == 2
    cut, whole = records

    assert cut["truncated"] is True
    assert whole["truncated"] is False

    # The reconstructed usage is a lower bound: the stream ended before the
    # message_delta that carries output_tokens.
    assert cut["usage"]["output_tokens"] == 0
    assert whole["usage"]["output_tokens"] == 500

    # And the latency is time-to-disconnect, not time-to-completion — which is
    # why summarize() keeps truncated calls out of the percentiles.
    assert cut["latency_ms"] < whole["latency_ms"]
