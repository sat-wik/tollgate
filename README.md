# Tollgate

A local capture proxy for LLM API traffic. Point your app's base URL at
Tollgate and every call is forwarded to the real provider unchanged — auth
headers pass through, nothing is stored server-side, no keys are held — while a
copy of each request/response pair lands in a local JSONL file, priced.

The point: **most apps can't tell you what a given request cost.** Tollgate
gives you per-request token and dollar attribution with a one-line config
change, without touching your code or sitting in any routing decision. It
observes; it never redirects.

## Quick start

```sh
pip install tollgate-proxy   # or: pip install -e .
tollgate                     # listens on 127.0.0.1:4141
```

Point your client at it:

```python
# OpenAI
client = OpenAI(base_url="http://127.0.0.1:4141/v1")

# Anthropic
client = anthropic.Anthropic(base_url="http://127.0.0.1:4141")
```

Calls flow through to `api.openai.com` / `api.anthropic.com` as usual; captures
append to `~/.tollgate/captured.jsonl`.

```sh
tollgate report
```

```
model             calls  input_tokens  output_tokens  cache_read_tokens  cost_usd  latency_p50_ms  ttft_p50_ms
----------------  -----  ------------  -------------  -----------------  --------  --------------  -----------
claude-opus-5     2      23,500        1,440          8,000              0.1575    3,900.00        290.00
gpt-4o            1      6,000         500            2,000              0.0225    2,100.00        -
claude-haiku-4-5  1      12,000        810            4,000              0.0164    900.00          95.00
```

## Why another proxy

**Real LLM traffic is streamed.** Proxies that buffer or reject `stream: true`
capture nothing in practice. Tollgate relays SSE chunks the moment they
arrive — your app sees identical streaming behavior — and reconstructs the full
response for the log only after the stream closes. Streamed calls get a
time-to-first-byte measurement alongside end-to-end latency.

**Usage doesn't come back in one shape.** Anthropic reports `input_tokens` and
`cache_creation_input_tokens`; OpenAI chat reports `prompt_tokens` *inclusive*
of `prompt_tokens_details.cached_tokens`; the Responses API does it a third
way. Tollgate normalizes all of them into one four-field shape, so a mixed-
provider log is summable.

**Cost is derived, not stored.** Every report recomputes usage and price from
the raw pair, so old logs re-price correctly when rates change — and a model
with no known price reports `null`, never `$0.00`.

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
{"id": "9f2c…", "timestamp": "2026-07-27T18:04:11.480+00:00",
 "endpoint": "/v1/messages", "provider": "anthropic", "model": "claude-opus-5",
 "stream": true, "status": 200, "latency_ms": 3912.4, "ttft_ms": 288.1,
 "usage": {"input_tokens": 11500, "output_tokens": 640,
           "cache_read_tokens": 4000, "cache_write_tokens": 0},
 "cost_usd": 0.0757,
 "prompt_sha": "e376401292451955", "request_sha": "a1c4…",
 "request": {…}, "response": {…}}
```

`request` and `response` are the verbatim pair — for streamed calls the
response is reconstructed from the SSE events, so downstream tools see the same
shape either way. Everything else is derived from that pair and recomputed on
every read.

Two fingerprints make change analysis possible. `prompt_sha` covers only
`system` / `messages` / `input` / `tools`, so it's stable across a model swap —
group by it to compare the same prompt on different models. `request_sha`
covers the whole body and moves when anything does.

## Replay

The raw pairs are the substrate for two kinds of analysis.

**Offline** is deterministic and never touches the network — the same log and
price table always produce the same numbers, which is what makes a cost figure
auditable months later:

```sh
tollgate replay                          # recompute from the log
tollgate report --group-by prompt_sha    # one row per distinct prompt
tollgate report --json                   # for a dashboard
```

**Live** re-issues the logged requests against the provider and diffs the
result against the recorded baseline — so "is Haiku cheap enough here?" is a
measurement, not an estimate:

```sh
tollgate replay --live --model claude-haiku-4-5 --limit 50
```

```
id                baseline_model  replay_model      cost_delta_usd  latency_delta_ms
----------------  --------------  ----------------  --------------  ----------------
9f2c1a4b…         claude-opus-5   claude-haiku-4-5  -0.0712         -2,940.00
```

Live replay costs real money and needs `ANTHROPIC_API_KEY` / `OPENAI_API_KEY`
in the environment — Tollgate never stores the credentials from captured
traffic, so it can't reuse them. Requests are replayed non-streamed so both
sides of the diff are one complete body.

## Pricing table

Rates are per million tokens, current as of **2026-07**, and cover the current
Anthropic and OpenAI model lines. Cached tokens bill at a multiple of the input
rate (Anthropic: 0.1x on read, 1.25x on a 5-minute write).

Two caveats worth knowing: Claude Sonnet 5 carries an introductory $2.00/$10.00
rate through 2026-08-31 and the table lists the standard $3.00/$15.00; and
OpenAI list prices move often, so verify them before treating a number as
billing-grade. Both are one file away:

```sh
TOLLGATE_PRICING=~/prices.json tollgate report
```

```json
{"claude-sonnet-5": {"input": 2.00, "output": 10.00}}
```

Entries merge over the built-in table, and a dated snapshot inherits its alias's
price (`claude-haiku-4-5-20251001` prices as `claude-haiku-4-5`).

## Trust model

Local-first. Logs never leave your machine; requests go only to the provider
your app was already calling, authenticated with your app's own headers.
Credentials are forwarded upstream and never written to the log.

## License

MIT
