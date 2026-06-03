# CLAUDE.md — Tollgate

This file is the operating manual for Claude Code working in this repo. Read it fully before starting any task. The companion document is `tollgate-prd.md`; that defines **what** v1 is, this file defines **how** to build it and **what not to do**.

-----

## 0. Purpose of this file

Keep work on the v1 thesis (transparent local proxy + deterministic analysis) and prevent drift into year-two features (model-based classification, agent-loop interception, governance). If a request seems to push past v1, **stop and ask** instead of expanding scope.

-----

## 1. Project at a glance

Tollgate is a localhost HTTP server that **looks like the Anthropic Messages API and the OpenAI Chat Completions API to its clients**, forwards their requests to the real upstream, and on the way through, captures token counts, cost estimates, lint findings, and caching opportunities — all using deterministic local logic, no extra model calls. A separate read-only UI (local web dashboard first; VS Code panel later) renders what the proxy has stored.

The product’s defensibility comes from being **transparent, local, and provider-agnostic**. Any change that compromises one of those three properties is a regression, not a feature.

-----

## 2. Architectural invariants (must always hold)

These are non-negotiable for v1. If a task appears to require violating one, escalate before coding.

1. **Transparency.** Request and response bodies forwarded to the upstream are byte-equivalent to what the client sent / received, except for the addition of HTTP headers the upstream ignores. No silent rewriting of prompts. (Auto-stripping duplicates is gated behind an explicit, default-off flag — and not in v1.)
1. **Streaming preserved.** SSE / chunked responses must stream through with no buffering of full bodies before forwarding. End-to-end first-token latency added by the proxy: target < 50 ms.
1. **No outbound calls for analysis.** Token counting, linting, caching detection, pricing, and dashboards must work fully offline. The only outbound traffic Tollgate generates is the forwarded user request.
1. **No raw prompt content stored by default.** Persistence is metadata + content hashes only. Raw logging is a per-route opt-in flag, surfaced loudly in config.
1. **Provider-agnostic core.** Provider-specific code lives only in `adapters/`. The rest of the system operates on a normalized internal request type.
1. **No LLM dependency in v1.** Not even local (Ollama). The semantic pre-pass is a fast-follow, deliberately deferred.

-----

## 3. Tech choices

- **Language:** TypeScript on Node 20+. Reasons: streaming HTTP is straightforward, the OpenAI/Anthropic SDK shapes are easy to model in TS, `tiktoken` has a maintained Node binding, and it matches the author’s stack. Go is a reasonable v2 rewrite target if perf needs it; do not start in Go.
- **HTTP server:** Fastify (low overhead, first-class streaming, schema validation). Do not use Express.
- **Persistence:** SQLite via `better-sqlite3` (synchronous, fast, no server). Single file at `~/.tollgate/tollgate.db`.
- **Tokenization:** `tiktoken` for OpenAI models; for Anthropic, ship an approximation function and document the accuracy band. Provide a hook to swap in exact counting later.
- **Config:** TOML at `~/.tollgate/config.toml`, hot-reloadable.
- **Testing:** Vitest. Streaming tests use a fixture upstream (a tiny local SSE server) — do not hit real providers in CI.
- **Build/distribution:** `pkg` or `bun build --compile` to produce a single binary later; for v1 a `node` entrypoint and `npx tollgate` is fine.

-----

## 4. Repo layout

```
src/
  server/            # Fastify wiring, routes, streaming plumbing
  adapters/
    anthropic.ts     # parse/serialize Anthropic Messages API
    openai.ts        # parse/serialize OpenAI Chat Completions API
    types.ts         # NormalizedRequest, NormalizedResponse
  tokenizer/
    openai.ts        # tiktoken wrapper
    anthropic.ts     # approximation + accuracy notes
    index.ts         # dispatch by model
  pricing/
    table.json       # bundled defaults
    index.ts         # lookup + override merge
  lint/
    rules/           # one file per rule (see §6.4)
    engine.ts        # runs all rules, returns Finding[]
  cache/
    detector.ts      # prefix-match against rolling window
    window.ts        # in-memory ring of hashed prefixes
  budget/
    tracker.ts       # session + daily counters, threshold checks
  store/
    schema.sql
    repo.ts          # typed access layer over SQLite
  ui/
    web/             # local dashboard (read-only)
config/
  default.toml
test/
  fixtures/          # recorded request/response pairs for both providers
  upstream-mock.ts   # local SSE upstream for streaming tests
```

Add new files only where they fit this layout. If a new top-level concept appears, surface it before adding a new directory.

-----

## 5. Build order (do not skip ahead)

Each milestone is gated by its definition of done. Do not begin the next milestone until the prior one passes its DoD in CI.

### M1 — Transparent proxy

- Fastify server with two routes: `POST /v1/messages` (Anthropic) and `POST /v1/chat/completions` (OpenAI).
- Forwards to the upstream (configured per route) with auth header passthrough.
- Streams responses through chunk-by-chunk.
- Captures: timestamp, model, input/output token counts (from the response’s `usage` field for now, not yet estimated), upstream latency, route label. Writes one row per request to `requests`.
- **DoD:** Claude Code (configured with `ANTHROPIC_BASE_URL=http://localhost:PORT`) completes a real session through Tollgate with identical behavior to direct mode; streaming first-token overhead < 50 ms on localhost; no request/response body diffs in fixture replay tests.

### M2 — Pre-flight estimate + budgets

- Tokenize input locally **before** forwarding. Persist the estimate alongside the actual usage and report the delta in the dashboard for accuracy tuning.
- Pricing table lookup → cost estimate.
- Budget tracker: per-session and per-day counters, threshold events (80% / 100%).
- **DoD:** estimate-vs-actual delta within documented band (OpenAI: exact; Anthropic: ±10%) across the fixture set; budgets verified in unit tests.

### M3 — Lint engine + cache detector

- Implement the four lint rules from §6.4.
- Implement prefix-match cache detector against a rolling window (size configurable, default last 100 requests per route).
- Findings persisted per request.
- **DoD:** every rule has a positive and negative fixture; deliberately wasteful prompts produce at least one finding each.

### M4 — Dashboard + receipts

- Local web dashboard at `http://localhost:PORT/_tollgate` (different path prefix from the API routes to avoid collision): spend over time, breakdown by model / route / request-type, list view of recent requests with their findings.
- Per-response receipt endpoint (`GET /_tollgate/receipt/:request_id`).
- **DoD:** dashboard renders correctly from a seeded DB; receipt accurately reflects stored data.

### M5 — Polish

- One-command install path documented (`npx tollgate init`).
- Config docs with every option, defaults, and security notes.
- Accuracy calibration note for Anthropic tokenizing.

-----

## 6. Module contracts

These are load-bearing types. **Do not change them without updating this file in the same PR.**

### 6.1 Normalized request / response

```ts
// adapters/types.ts
export type NormalizedMessage = {
  role: "system" | "user" | "assistant" | "tool";
  // Content normalized to an array of parts to handle multimodal uniformly.
  content: Array<
    | { type: "text"; text: string }
    | { type: "image"; mediaType: string; bytes: number }
    | { type: "document"; mediaType: string; bytes: number; pages?: number }
    | { type: "tool_use"; name: string; input: unknown }
    | { type: "tool_result"; toolUseId: string; content: string }
  >;
};

export type NormalizedRequest = {
  provider: "anthropic" | "openai";
  model: string;
  messages: NormalizedMessage[];
  maxOutputTokens?: number;
  // Anything provider-specific lives here, opaque to the core.
  providerExtras: Record<string, unknown>;
  // The exact original body, used for byte-equivalent forwarding.
  rawBody: Buffer;
};
```

Adapters convert provider-native bodies ↔ `NormalizedRequest`. The core never sees raw provider bodies except as `rawBody` (write-only from the core’s perspective).

### 6.2 Tokenizer

```ts
// tokenizer/index.ts
export interface Tokenizer {
  countMessages(req: NormalizedRequest): { inputTokens: number; accuracy: "exact" | "approx" };
}
```

One implementation per provider family. Returning `accuracy` lets the UI honestly label estimates.

### 6.3 Pricing

```ts
// pricing/table.json shape
{
  "anthropic/claude-opus-4-7": { "inputPerMTok": 15.00, "outputPerMTok": 75.00, "cachedInputPerMTok": 1.50 },
  "openai/gpt-4o":              { "inputPerMTok":  2.50, "outputPerMTok": 10.00 }
}
```

```ts
// pricing/index.ts
export function priceRequest(model: string, input: number, output: number, cachedInput?: number): {
  inputCost: number; outputCost: number; cachedDiscount: number; total: number; currency: "USD";
};
```

Pricing values in the JSON must be cited inline as comments with date + source. **Do not** guess pricing — if a model isn’t in the table, return `unknown` and surface that in the UI.

### 6.4 Lint rule

```ts
// lint/rules/types.ts
export type Finding = {
  rule: string;                  // stable kebab-case id, e.g. "duplicate-block"
  severity: "info" | "warn" | "high";
  tokensWastedEst: number;       // 0 if not estimable
  message: string;               // user-facing, specific, actionable
  location?: { messageIndex: number; partIndex?: number; charRange?: [number, number] };
};

export interface LintRule {
  id: string;
  run(req: NormalizedRequest, ctx: LintContext): Finding[];
}
```

The four v1 rules, each in its own file:

- `duplicate-block.ts` — normalized-text near-duplicates within one request (use a shingled hash, not exact string match).
- `oversized-paste.ts` — single text part above threshold (default 4k tokens; configurable).
- `multimodal-surprise.ts` — image/document parts with their token estimate spelled out; flag if total multimodal tokens > threshold.
- `stale-history.ts` — assistant/user turns beyond depth N (default 20) with cumulative token cost.

Lint rules are **pure functions of the request**. No I/O, no clock, no global state.

### 6.5 Cache detector

```ts
export interface CacheDetector {
  observe(req: NormalizedRequest, hash: string): void;
  detect(req: NormalizedRequest): { repeatedTokens: number; matchedRequestId: string } | null;
}
```

Prefix similarity is computed on the normalized text concatenation of system + leading user message(s), not on raw bodies.

### 6.6 Storage

`schema.sql`:

```sql
CREATE TABLE requests (
  id TEXT PRIMARY KEY,
  ts INTEGER NOT NULL,
  provider TEXT NOT NULL,
  model TEXT NOT NULL,
  route_label TEXT,
  input_tokens_est INTEGER,
  input_tokens_actual INTEGER,
  output_tokens_actual INTEGER,
  est_input_cost REAL,
  est_output_cost REAL,
  upstream_ms INTEGER,
  content_hash TEXT,
  raw_logged INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE findings (
  request_id TEXT NOT NULL REFERENCES requests(id),
  rule TEXT NOT NULL,
  severity TEXT NOT NULL,
  tokens_wasted_est INTEGER NOT NULL,
  message TEXT NOT NULL,
  location_json TEXT
);
CREATE INDEX idx_requests_ts ON requests(ts);
```

All money values stored in USD as floats with two-decimal display rounding done in the UI, not at write time.

-----

## 7. Streaming — read this before touching the proxy

- Use `reply.raw` (Node `ServerResponse`) and pipe directly. Do not call `reply.send(...)` on streaming responses.
- Capture token usage from the **final** SSE event (Anthropic: `message_delta` with `usage`; OpenAI: `[DONE]` preceded by the last chunk with `usage` when `stream_options.include_usage` is set — note for OpenAI we must inject this option if the client didn’t, and document this as the one allowed body modification).
- The above is the **only** sanctioned request mutation in v1. It is opt-out-able by config.
- Backpressure: respect `res.write` returning `false`; do not buffer chunks.
- Every streaming code path needs a test against `test/upstream-mock.ts`.

-----

## 8. Testing expectations

- **Fixture replay**: capture real request/response pairs (with secrets scrubbed) and assert byte-equivalence of the forwarded request and the relayed response.
- **Property test**: round-tripping `provider-body → NormalizedRequest → provider-body` is the identity (modulo the OpenAI usage-injection noted above).
- **Lint rules**: pos + neg fixture per rule.
- **No live provider calls in CI.**

-----

## 9. Privacy & logging rules

- `raw_logged` defaults to 0. Never flip it without an explicit config opt-in per route.
- Hashes are SHA-256 of the normalized text content; never log hash inputs.
- The dashboard never displays raw content unless `raw_logged = 1` for that row.
- Crash logs and error reports must redact request bodies.

-----

## 10. Out of scope for v1 — do not build

If a task implies any of these, stop and ask:

- Any feature that requires running a local or remote model (semantic classification, “this looks like a Haiku task”, rewrite suggestions beyond the deterministic lint messages).
- Speculative cheap-first execution / response substitution.
- Agent-loop thrashing detection or any mid-loop intervention.
- Team / org features: shared budgets, policy enforcement, multi-tenant auth, CI hooks.
- Modifying request bodies beyond the single sanctioned OpenAI `include_usage` injection.
- VS Code extension UI (the local web dashboard is the v1 surface; extension is a later milestone).
- Auto-updating the pricing table over the network.

-----

## 11. Decision log

Append-only. Every architectural decision that’s non-obvious goes here with a date and one-line rationale.

- *2026-06-03* — Chose TS/Node over Go for v1: streaming + tokenizer ergonomics + author stack; Go is a viable v2 rewrite.
- *2026-06-03* — Allowed the single mutation of injecting `stream_options.include_usage` for OpenAI streaming, opt-out via config. Required to get token counts without a second request.
- *2026-06-03* — M1 forwarding uses the captured raw request body (`rawBody`) piped through Node's `http`/`https` with `reply.hijack()`, guaranteeing byte-equivalence rather than re-serializing from `NormalizedRequest`. Adapters are parse-only for now; provider-body serialization is deferred until a milestone needs it.
- *2026-06-03* — Usage capture reads from a private copy of the response (decompressed if needed) and never alters the bytes streamed to the client. Streaming stays live; the copy is parsed only at stream end. Accepted bounded memory for the copy in v1.
