# Tollgate

A local, provider-agnostic LLM proxy that estimates the cost of every request
before it goes out, lints prompts for waste, and gives you a personal spend
dashboard — all running on your machine, for free, with no prompts leaving your
control for analysis.

See [`tollgate-prd.md`](./tollgate-prd.md) for the product spec and
[`CLAUDE.md`](./CLAUDE.md) for the engineering operating manual.

## Status

**M1 — Transparent proxy** ✅ &nbsp;·&nbsp; **M2 — Pre-flight estimate + budgets** ✅ &nbsp;·&nbsp; **M3 — Lint engine + cache detector** ✅ &nbsp;·&nbsp; **M4 — Dashboard + receipts** ✅ (in progress toward v1)

Tollgate exposes provider-compatible endpoints, forwards requests to the real
upstream with auth passthrough, streams responses through chunk-by-chunk, and
records per-request metadata (model, token usage, latency, content hash) to a
local SQLite store. Request/response bodies are forwarded byte-for-byte; the
only sanctioned mutation is injecting `stream_options.include_usage` on
streaming OpenAI requests (opt-out via config).

M2 adds local **pre-flight token estimation** before each request is forwarded
(`tiktoken` for OpenAI — exact for text; a calibrated o200k-based approximation
for Anthropic — within a documented ±10% band), a **pricing table** with cost
estimation, and **per-session/daily budgets** with threshold warnings (80% /
100%; v1 warns but does not block). The estimate is surfaced as `x-tollgate-*`
response headers and persisted alongside the provider's actual usage.

M3 adds a deterministic **context-hygiene lint** (duplicate blocks, oversized
pastes, multimodal cost surprises, stale history) and a **caching-opportunity
detector** that flags when a request shares a long prefix with a recent one —
all local, no model calls. Findings are persisted per request and surfaced via
`x-tollgate-lint-findings` headers.

M4 adds a read-only **local web dashboard** at `http://127.0.0.1:PORT/_tollgate`
(spend over time; breakdowns by model, route, and request type; recent requests
with their findings) and a per-response **receipt** endpoint at
`/_tollgate/receipt/:id` (input vs. output cost split, top cost driver,
findings). The page is fully self-contained and works offline.

All five v1 milestones (M1–M5) are implemented.

## Requirements

- Node.js >= 20

## Quick start

```bash
npm install
npm run build           # compile to dist/ and copy runtime assets
npx tollgate init       # scaffold ~/.tollgate/config.toml + print setup steps
npx tollgate            # start the proxy on http://127.0.0.1:8787
```

`tollgate init` is idempotent — it never overwrites an existing config.

Then point your tools at the proxy:

```bash
# Claude Code / Anthropic SDK
export ANTHROPIC_BASE_URL=http://127.0.0.1:8787

# OpenAI SDK
export OPENAI_BASE_URL=http://127.0.0.1:8787/v1
```

…and open the dashboard at <http://127.0.0.1:8787/_tollgate>.

> Running from source without building? Use `npx tsx src/index.ts init` and
> `npm start`.

## Configuration

User overrides go in `~/.tollgate/config.toml`, merged over the bundled
[`config/default.toml`](./config/default.toml). The SQLite store defaults to
`~/.tollgate/tollgate.db`.

See the **[full configuration reference](./docs/configuration.md)** for every
option, default, and security note (auth passthrough, the `raw_log` privacy
opt-in, budgets, pricing overrides, lint/cache thresholds, and response headers).

## Accuracy & privacy

- Input tokens are counted locally before forwarding — exact for OpenAI text,
  and a calibrated ±10% approximation for Anthropic. See the
  **[Anthropic token-accuracy note](./docs/anthropic-token-accuracy.md)**.
- API keys pass straight through to the provider and are never stored. By
  default only request metadata + content hashes are persisted — never raw
  prompts (raw logging is a per-route opt-in).
- All analysis (tokenizing, pricing, lint, caching, dashboard) runs offline;
  the only outbound traffic is your forwarded request.

## Development

```bash
npm run typecheck   # tsc --noEmit
npm test            # vitest (no live provider calls; uses a local mock upstream)
npm run build       # emit dist/ (used by the `tollgate` bin)
```
