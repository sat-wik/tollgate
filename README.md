# Tollgate

A local, observe-only capture proxy for LLM API traffic. Point your app's base
URL at Tollgate and every call is forwarded to the real provider unchanged —
auth headers pass through, nothing is stored server-side, no keys are held —
while a copy of each request/response lands in a local JSONL file.

The point: **most apps don't log their LLM calls.** Tollgate gives you a
production log with a one-line config change, without touching your code or
sitting in any routing decision. It observes; it never redirects.

## Why another proxy

Real LLM traffic is streamed. Proxies that buffer or reject `stream: true`
capture nothing in practice. Tollgate relays SSE chunks the moment they
arrive — your app sees identical streaming behavior — and reconstructs the
full response for the log only after the stream closes.

## Quick start

```sh
pip install tollgate-proxy   # or: pip install -e .
tollgate --port 4141
```

Then point your client at it:

```python
# OpenAI
client = OpenAI(base_url="http://127.0.0.1:4141/v1")

# Anthropic
client = anthropic.Anthropic(base_url="http://127.0.0.1:4141")
```

Calls flow through to `api.openai.com` / `api.anthropic.com` as usual;
captures append to `~/.tollgate/captured.jsonl`.

## Supported endpoints

| Endpoint | Upstream | Streaming |
|---|---|---|
| `POST /v1/chat/completions` | OpenAI | ✅ SSE passthrough |
| `POST /v1/responses` | OpenAI | ✅ SSE passthrough |
| `POST /v1/messages` | Anthropic | ✅ SSE passthrough |

Override upstreams with `TOLLGATE_UPSTREAM_OPENAI` / `TOLLGATE_UPSTREAM_ANTHROPIC`
(useful for Azure-style gateways or test servers).

## Log format

One JSON object per line:

```json
{"timestamp": "...", "endpoint": "/v1/messages",
 "request": {"model": "...", "messages": [...]},
 "response": {"content": [...], "usage": {...}}}
```

For streamed calls the `response` is reconstructed from the SSE events
(accumulated text + usage), so downstream tools see the same shape either way.

Tollgate is the capture component of [Vouch](https://github.com/sat-wik/vouch),
a regression suite for LLM calls that writes itself from production traffic —
`vouch analyze --logs ~/.tollgate/captured.jsonl` clusters a Tollgate log
directly. It works standalone for anyone who just wants their traffic on disk.

## Trust model

Local-first. Logs never leave your machine; requests go only to the provider
your app was already calling, authenticated with your app's own headers.

## License

MIT
