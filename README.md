# Tollgate

A local, provider-agnostic LLM proxy that estimates the cost of every request
before it goes out, lints prompts for waste, and gives you a personal spend
dashboard — all running on your machine, for free, with no prompts leaving your
control for analysis.

See [`tollgate-prd.md`](./tollgate-prd.md) for the product spec and
[`CLAUDE.md`](./CLAUDE.md) for the engineering operating manual.

## Status

**M1 — Transparent proxy** ✅ (in progress toward v1)

Tollgate exposes provider-compatible endpoints, forwards requests to the real
upstream with auth passthrough, streams responses through chunk-by-chunk, and
records per-request metadata (model, token usage, latency, content hash) to a
local SQLite store. Request/response bodies are forwarded byte-for-byte; the
only sanctioned mutation is injecting `stream_options.include_usage` on
streaming OpenAI requests (opt-out via config).

Roadmap: M2 pre-flight estimate + budgets → M3 lint engine + cache detector →
M4 dashboard + receipts → M5 polish.

## Requirements

- Node.js >= 20

## Install & run

```bash
npm install
npm start          # starts the proxy on http://127.0.0.1:8787
```

Point your tools at the proxy:

```bash
# Claude Code / Anthropic SDK
export ANTHROPIC_BASE_URL=http://127.0.0.1:8787

# OpenAI SDK
export OPENAI_BASE_URL=http://127.0.0.1:8787/v1
```

## Configuration

Defaults live in [`config/default.toml`](./config/default.toml). User overrides
go in `~/.tollgate/config.toml` and are merged on top. Options include the
listen port, per-route upstreams and labels, the `raw_log` privacy opt-in
(default off — only metadata + content hashes are stored), and the OpenAI
`inject_usage` toggle.

The SQLite store defaults to `~/.tollgate/tollgate.db`.

## Development

```bash
npm run typecheck   # tsc --noEmit
npm test            # vitest (no live provider calls; uses a local mock upstream)
```
