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

import asyncio
import json
import os
import sys
import time
from contextlib import asynccontextmanager
from typing import Any, Callable, Iterator

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import Response, StreamingResponse

from .record import CaptureWriter, build_record

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
        choices = ev.get("choices")
        for choice in choices if isinstance(choices, list) else []:
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta")
            if isinstance(delta, dict) and isinstance(delta.get("content"), str):
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
            message = ev.get("message")
            if isinstance(message, dict):
                model = message.get("model")
                if isinstance(message.get("usage"), dict):
                    usage.update(message["usage"])
        elif kind == "content_block_delta":
            delta = ev.get("delta")
            if isinstance(delta, dict) and delta.get("type") == "text_delta":
                text = delta.get("text")
                if isinstance(text, str):
                    text_parts.append(text)
        elif kind == "message_delta":
            if isinstance(ev.get("usage"), dict):
                usage.update(ev["usage"])
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


_capture_warned = False


def warn_once(exc: BaseException, path: str) -> None:
    global _capture_warned
    if not _capture_warned:
        _capture_warned = True
        print(
            f"tollgate: capture failed ({type(exc).__name__}: {exc}) for {path}; "
            "requests are still being proxied, but they are not being logged",
            file=sys.stderr,
        )


def capture(writer: CaptureWriter, build: Callable[[], dict[str, Any]]) -> None:
    """Build and write a record, and never let either step break the call.

    Tollgate sits in the request path of an application that would work fine
    without it. A full disk, a read-only mount, a permissions change, or a
    malformed event that trips the SSE reconstruction must not turn a
    successful provider response — already generated, already billed — into a
    failure for the caller. Losing the observation is bad; losing the request
    is worse.

    `build` is a callable rather than a record so that reconstructing and
    pricing happen inside the guard too. Those are the parts most likely to
    meet input nobody anticipated, and on the streaming path they run inside
    the relay generator's teardown, where an exception breaks the caller's
    stream mid-flight.

    The record is handed to a background writer rather than written here.
    `log_record` takes an exclusive file lock, and a lock is a blocking
    syscall: if a second Tollgate process holds it, waiting for the write
    delays every response, including ones with nothing to log. Measured at 16x
    on 40 concurrent requests against a lock held elsewhere — and a proxy that
    inflates latency under contention is corrupting the number it exists to
    report. Building the record stays inline; it is CPU-only and fast.

    The loss is reported once per process rather than per request, so a
    misconfigured path is visible without flooding the log of the app being
    observed.
    """
    try:
        writer.submit(build())
    except Exception as exc:  # noqa: BLE001 - capture must never propagate
        warn_once(exc, writer.path)


def _transport_error_body(exc: Exception) -> dict[str, Any]:
    return {
        "error": {
            "type": "upstream_connection_error",
            "message": f"{type(exc).__name__}: {exc}",
        }
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
    writer = CaptureWriter(capture_path)
    writer.start(on_error=lambda exc: warn_once(exc, capture_path))
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
            # Drain anything still queued before the process goes away.
            writer.stop()

    app = FastAPI(title="Tollgate", docs_url=None, redoc_url=None, lifespan=lifespan)
    # Exposed so an embedder (or a test) can force queued records to disk
    # before reading the log; capture is asynchronous by design.
    app.state.capture_writer = writer

    def _unreachable(
        endpoint: str,
        req_json: dict[str, Any],
        exc: Exception,
        started: float,
        *,
        stream: bool = False,
    ) -> Response:
        """Record an upstream we could not reach, and tell the caller so."""
        body = _transport_error_body(exc)
        capture(
            writer,
            lambda: build_record(
                endpoint,
                req_json,
                body,
                status=502,
                stream=stream,
                latency_ms=(time.perf_counter() - started) * 1000,
            ),
        )
        return Response(
            content=json.dumps(body).encode(),
            status_code=502,
            media_type="application/json",
        )

    def get_client() -> httpx.AsyncClient:
        # Sub-mounted or embedded apps don't always get their lifespan run, and
        # a KeyError at request time is a poor way to find that out. Fall back
        # to creating the pooled client on first use; the lifespan still owns
        # closing it.
        if "client" not in state:
            state["client"] = httpx.AsyncClient(timeout=600.0)
        return state["client"]

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
        client = get_client()
        started = time.perf_counter()

        if req_json.get("stream"):
            upstream = client.stream("POST", url, content=body, headers=headers)
            try:
                resp = await upstream.__aenter__()
            except httpx.HTTPError as exc:
                return _unreachable(endpoint, req_json, exc, started, stream=True)
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

                    def record():
                        # A failed stream carries a JSON error body, not SSE.
                        reconstructed = (
                            ACCUMULATORS[endpoint](raw)
                            if resp.status_code == 200
                            else _error_body(raw)
                        )
                        return build_record(
                            endpoint,
                            req_json,
                            reconstructed,
                            status=resp.status_code,
                            stream=True,
                            latency_ms=elapsed,
                            ttft_ms=ttft,
                            truncated=not completed,
                        )

                    capture(writer, record)

            return StreamingResponse(
                relay(),
                status_code=resp.status_code,
                media_type=resp.headers.get("content-type", "text/event-stream"),
            )

        try:
            resp = await client.post(url, content=body, headers=headers)
        except httpx.HTTPError as exc:
            # The provider was never reached. That is exactly the kind of event
            # a reliability log exists to show, so record it rather than let it
            # vanish as an unhandled exception.
            return _unreachable(endpoint, req_json, exc, started)
        elapsed = (time.perf_counter() - started) * 1000
        try:
            resp_json = resp.json()
        except (json.JSONDecodeError, ValueError):
            resp_json = _error_body(resp.text) if resp.status_code != 200 else None
        if resp_json is not None:
            capture(
                writer,
                lambda: build_record(
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
