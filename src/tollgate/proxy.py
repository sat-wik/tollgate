"""The Tollgate proxy: forwards LLM API calls unchanged and tees them to JSONL.

Observes; never redirects. Point your app's base URL at Tollgate and it
forwards /v1/chat/completions and /v1/responses to OpenAI and /v1/messages to
Anthropic, streaming SSE through untouched — chunks are relayed the moment
they arrive, and the response is reconstructed for the log only after the
stream closes. Auth headers pass through; Tollgate stores no keys.

Log format (one JSON object per line):
    {"timestamp": ..., "endpoint": ..., "request": <verbatim request body>,
     "response": <response body, reconstructed for streams>}
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Iterator

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import Response, StreamingResponse

UPSTREAMS = {
    "/v1/chat/completions": os.environ.get("TOLLGATE_UPSTREAM_OPENAI", "https://api.openai.com"),
    "/v1/responses": os.environ.get("TOLLGATE_UPSTREAM_OPENAI", "https://api.openai.com"),
    "/v1/messages": os.environ.get("TOLLGATE_UPSTREAM_ANTHROPIC", "https://api.anthropic.com"),
}

_HOP_HEADERS = {"host", "content-length", "connection", "accept-encoding"}


def default_capture_path() -> str:
    return os.path.join(os.path.expanduser("~/.tollgate"), "captured.jsonl")


# -- SSE accumulation (pure functions) ---------------------------------------


def sse_data_events(raw: str) -> Iterator[dict[str, Any]]:
    for line in raw.splitlines():
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            yield obj


def accumulate_openai_chat(raw: str) -> dict[str, Any]:
    """Reconstruct a chat.completion response from a chat stream."""
    text_parts: list[str] = []
    model = None
    usage: dict[str, Any] = {}
    for ev in sse_data_events(raw):
        model = ev.get("model") or model
        if isinstance(ev.get("usage"), dict):
            usage = ev["usage"]
        for choice in ev.get("choices") or []:
            delta = choice.get("delta") or {}
            if isinstance(delta.get("content"), str):
                text_parts.append(delta["content"])
    return {
        "model": model,
        "choices": [{"message": {"role": "assistant", "content": "".join(text_parts)}}],
        "usage": usage,
    }


def accumulate_anthropic(raw: str) -> dict[str, Any]:
    """Reconstruct an Anthropic message response from an SSE stream."""
    text_parts: list[str] = []
    model = None
    usage: dict[str, Any] = {}
    for ev in sse_data_events(raw):
        kind = ev.get("type")
        if kind == "message_start":
            message = ev.get("message") or {}
            model = message.get("model")
            usage.update(message.get("usage") or {})
        elif kind == "content_block_delta":
            delta = ev.get("delta") or {}
            if delta.get("type") == "text_delta":
                text_parts.append(delta.get("text", ""))
        elif kind == "message_delta":
            usage.update(ev.get("usage") or {})
    return {
        "model": model,
        "content": [{"type": "text", "text": "".join(text_parts)}],
        "usage": usage,
    }


def accumulate_openai_responses(raw: str) -> dict[str, Any]:
    """Reconstruct a Responses API result from its event stream."""
    text_parts: list[str] = []
    final: dict[str, Any] | None = None
    for ev in sse_data_events(raw):
        kind = ev.get("type", "")
        if kind == "response.output_text.delta" and isinstance(ev.get("delta"), str):
            text_parts.append(ev["delta"])
        elif kind == "response.completed" and isinstance(ev.get("response"), dict):
            final = ev["response"]
    if final is not None:
        return final
    return {"output_text": "".join(text_parts)}


ACCUMULATORS = {
    "/v1/chat/completions": accumulate_openai_chat,
    "/v1/messages": accumulate_anthropic,
    "/v1/responses": accumulate_openai_responses,
}


def log_record(path: str, endpoint: str, request: dict, response: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "endpoint": endpoint,
        "request": request,
        "response": response,
    }
    with open(path, "a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


# -- the proxy app -----------------------------------------------------------


def create_app(capture_path: str | None = None) -> FastAPI:
    capture_path = capture_path or default_capture_path()
    app = FastAPI(title="Tollgate", docs_url=None, redoc_url=None)

    async def proxy(request: Request, endpoint: str):
        body = await request.body()
        try:
            req_json = json.loads(body)
        except json.JSONDecodeError:
            req_json = {}
        headers = {
            k: v for k, v in request.headers.items() if k.lower() not in _HOP_HEADERS
        }
        url = UPSTREAMS[endpoint] + endpoint
        client = httpx.AsyncClient(timeout=600.0)

        if req_json.get("stream"):
            upstream = client.stream("POST", url, content=body, headers=headers)
            resp = await upstream.__aenter__()
            captured: list[bytes] = []

            async def relay():
                try:
                    async for chunk in resp.aiter_bytes():
                        captured.append(chunk)
                        yield chunk
                finally:
                    await upstream.__aexit__(None, None, None)
                    await client.aclose()
                    if resp.status_code == 200:
                        raw = b"".join(captured).decode("utf-8", "replace")
                        log_record(
                            capture_path, endpoint, req_json,
                            ACCUMULATORS[endpoint](raw),
                        )

            return StreamingResponse(
                relay(),
                status_code=resp.status_code,
                media_type=resp.headers.get("content-type", "text/event-stream"),
            )

        try:
            resp = await client.post(url, content=body, headers=headers)
        finally:
            await client.aclose()
        if resp.status_code == 200:
            try:
                log_record(capture_path, endpoint, req_json, resp.json())
            except json.JSONDecodeError:
                pass
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            media_type=resp.headers.get("content-type", "application/json"),
        )

    for path in UPSTREAMS:
        app.post(path)(lambda request, _p=path: proxy(request, _p))

    return app
