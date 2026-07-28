"""The Tollgate proxy: forwards LLM API calls unchanged and tees them to JSONL.

Observes; never redirects. Point your app's base URL at Tollgate and it
forwards /v1/chat/completions and /v1/responses to OpenAI and /v1/messages to
Anthropic, streaming SSE through untouched — chunks are relayed the moment
they arrive, and the response is reconstructed for the log only after the
stream closes. Auth headers pass through; Tollgate stores no keys.

Each captured call carries the raw request/response pair plus per-request
attribution: normalized token usage, dollar cost, end-to-end latency, and (for
streams) time to first byte. See `record.py` for the log schema.
"""

from __future__ import annotations

import json
import os
import time
from contextlib import asynccontextmanager
from typing import Any, Iterator

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import Response, StreamingResponse

from .record import build_record, log_record

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


def _error_body(raw: str) -> dict[str, Any]:
    """Wrap an upstream failure so it lands in the log as a record, not a gap.

    Rate limits, overloads, and 5xx are exactly the events you want to see in a
    latency report, and dropping them makes the log quietly incomplete.
    """
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        parsed = None
    if isinstance(parsed, dict):
        return parsed
    return {"error": {"message": raw[:2000]}}


# -- the proxy app -----------------------------------------------------------


def create_app(capture_path: str | None = None) -> FastAPI:
    capture_path = capture_path or default_capture_path()
    state: dict[str, httpx.AsyncClient] = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # One pooled client for the process lifetime. Opening a client per
        # request would put a fresh TCP and TLS handshake inside the window
        # this proxy is measuring, overstating provider latency by the
        # handshake on every single call.
        state["client"] = httpx.AsyncClient(timeout=600.0)
        try:
            yield
        finally:
            await state.pop("client").aclose()

    app = FastAPI(title="Tollgate", docs_url=None, redoc_url=None, lifespan=lifespan)

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
        client = state["client"]
        started = time.perf_counter()

        if req_json.get("stream"):
            upstream = client.stream("POST", url, content=body, headers=headers)
            resp = await upstream.__aenter__()
            captured: list[bytes] = []
            ttft: float | None = None

            async def relay():
                nonlocal ttft
                completed = False
                try:
                    async for chunk in resp.aiter_bytes():
                        if ttft is None:
                            ttft = (time.perf_counter() - started) * 1000
                        captured.append(chunk)
                        yield chunk
                    completed = True
                finally:
                    # If the client hung up, `yield` raised GeneratorExit and
                    # `completed` stayed False. The bytes so far are real and
                    # billed, but the usage and latency are cut short — record
                    # that rather than passing a partial call off as whole.
                    elapsed = (time.perf_counter() - started) * 1000
                    await upstream.__aexit__(None, None, None)
                    raw = b"".join(captured).decode("utf-8", "replace")
                    # A failed stream carries a JSON error body, not SSE.
                    reconstructed = (
                        ACCUMULATORS[endpoint](raw)
                        if resp.status_code == 200
                        else _error_body(raw)
                    )
                    log_record(
                        capture_path,
                        build_record(
                            endpoint,
                            req_json,
                            reconstructed,
                            status=resp.status_code,
                            stream=True,
                            latency_ms=elapsed,
                            ttft_ms=ttft,
                            truncated=not completed,
                        ),
                    )

            return StreamingResponse(
                relay(),
                status_code=resp.status_code,
                media_type=resp.headers.get("content-type", "text/event-stream"),
            )

        resp = await client.post(url, content=body, headers=headers)
        elapsed = (time.perf_counter() - started) * 1000
        try:
            resp_json = resp.json()
        except (json.JSONDecodeError, ValueError):
            resp_json = _error_body(resp.text) if resp.status_code != 200 else None
        if resp_json is not None:
            log_record(
                capture_path,
                build_record(
                    endpoint,
                    req_json,
                    resp_json,
                    status=resp.status_code,
                    latency_ms=elapsed,
                ),
            )
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            media_type=resp.headers.get("content-type", "application/json"),
        )

    def route_for(endpoint: str):
        # The handler's `request` parameter must be annotated `Request`, or
        # FastAPI reads it as a query parameter and rejects every call with a
        # 422 before the proxy ever runs.
        async def route(request: Request):
            return await proxy(request, endpoint)

        return route

    for path in UPSTREAMS:
        app.post(path)(route_for(path))

    return app
