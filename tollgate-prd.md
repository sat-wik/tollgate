# Tollgate — Product Requirements Document (v1)

> Working name: **Tollgate** (placeholder — it sits on the road between your tools and the model provider, measures everything that passes, and can stop traffic). Swap freely.

## One-liner

A local, provider-agnostic LLM proxy that estimates the cost of every request before it goes out, lints prompts for waste, and gives you a personal spend dashboard — all running on your machine, for free, with no prompts ever leaving your control for analysis.

-----

## 1. Problem

LLM-assisted coding tools (Claude Code, Cursor, Copilot agent mode) and hand-written SDK calls burn tokens in ways the user can’t see until the bill arrives. Existing tools (Tokenlint, tokencost, etc.) stop at *counting and warning* and only work on text you hand them directly. None of them can observe an agent loop, and the ones that “analyze” prompts often do so via a paid API call — a cost tool that costs money to run.

### Key architectural insight (this drives the whole design)

A VS Code extension **cannot** intercept prompts sent by other extensions (Copilot/Cursor/Claude Code) — those requests never leave the owning extension’s process. The only surface that can see *every* request, including agent loops, is a **local proxy** that tools route through via a custom base URL. Claude Code (`ANTHROPIC_BASE_URL`), the OpenAI SDK (`base_url`), Cursor, and most LLM libraries all support this. Tollgate is therefore a proxy first; any UI (CLI, web panel, or VS Code extension) is a thin viewer over what the proxy already knows.

This also means **the analysis is free and private**: token counting, cost estimation, lint heuristics, and caching detection all run locally with no extra API calls. That is the moat against API-based competitors.

-----

## 2. Goals

- Show an accurate **pre-flight cost estimate** for any request before it is forwarded to the provider.
- Detect and surface **wasted tokens** (duplicate context, oversized pastes, multimodal bloat) with actionable, specific messages.
- Detect **prompt-caching opportunities** and tell the user how to claim them.
- Enforce a **per-session budget** with threshold warnings.
- Provide a **spend dashboard** with attribution (by model, by project, by request type).
- Run entirely **locally**, work **offline** for analysis, and be **provider-agnostic** (Anthropic + OpenAI at minimum).

## 3. Non-goals (explicitly out of scope for v1)

- No semantic model-routing or “this should be Haiku” classification (needs a local model — fast-follow).
- No speculative cheap-first execution (latency + correctness risk — year two).
- No agent-loop circuit breaker / thrashing detection (hard, risky — year two).
- No team/org governance, policy enforcement, CI hooks, auth, or billing (separate product — year two).
- No prompt *rewriting* — v1 advises, it does not mutate request bodies (except optionally stripping detected exact-duplicate blocks behind a flag; see Open Questions).

## 4. Target user (v1)

Individual engineers who use Claude Code / Cursor / the SDKs heavily and want visibility and control over their own spend without sending their prompts to a third party. Self-serve, single-machine, low-infra.

-----

## 5. v1 Scope & Functional Requirements

### 5.1 Proxy core

- Expose localhost endpoints that are **drop-in compatible** with the Anthropic Messages API and the OpenAI Chat Completions API (request/response shapes pass through unchanged).
- Forward requests to the real upstream (provider base URL configurable per route) and stream responses back transparently — streaming must not be broken.
- Capture, for every request: timestamp, model, input token count, output token count, estimated cost, route/source label, and a content hash (not raw content, unless logging is opted in — see 5.6).
- Add negligible latency (target < 50 ms overhead on non-streamed paths).

### 5.2 Pre-flight cost estimation

- Count input tokens locally before forwarding: `tiktoken` for OpenAI models; a calibrated approximation for Anthropic models (document the accuracy band, ~90–95%).
- Multiply by current per-model pricing from a **local, updatable pricing table** (ship a default, allow user override; structure mirrors the `tokencost` data model).
- Emit the estimate via the chosen UI surface (and optionally as a response header for programmatic consumers).

### 5.3 Context-hygiene lint (deterministic heuristics, no model)

Each lint produces a typed finding `{rule, severity, tokens_wasted_est, message, location}`:

- **Duplicate blocks** — identical or near-identical (normalized) text segments repeated within one request.
- **Oversized paste** — a single contiguous block above a configurable token threshold (e.g. a whole file when likely only a snippet is needed).
- **Multimodal surprise** — image/PDF inputs with their token cost made explicit (“this PDF ≈ 15k tokens”).
- **Stale history** — conversation turns beyond a configurable depth, with their cumulative token cost.

### 5.4 Caching-opportunity detection

- Maintain a rolling window of recent request prefixes (hashed) per route.
- When a new request shares a long prefix with a recent one, surface: estimated repeated input tokens, the provider’s cache discount, and the concrete action (e.g. Anthropic cache breakpoints).

### 5.5 Budgets & warnings

- Configurable **per-session** and **daily** token/cost budgets.
- Warn at configurable thresholds (e.g. 80%, 100%); 100% produces a prominent warning but **does not block** in v1 (blocking is a flag, default off).

### 5.6 Spend dashboard & attribution

- Local persistence (SQLite) of the per-request records from 5.1.
- Views: spend over time; breakdown by model, by route/source label, and by a coarse request-type heuristic (e.g. “long-context”, “short-prompt”, “multimodal”).
- A per-response **receipt**: input vs output cost split, top cost driver, and any lint findings that applied.
- **Privacy default:** store metadata + hashes only. Raw prompt logging is strictly opt-in.

-----

## 6. Architecture (v1)

```
Claude Code / Cursor / SDK / curl
        │  (base_url -> localhost:PORT)
        ▼
┌─────────────────────────────────────┐
│  Tollgate proxy (local process)      │
│  ├─ request parser (Anthropic/OpenAI)│
│  ├─ local tokenizer + pricing table  │
│  ├─ lint engine (deterministic)      │
│  ├─ cache-opportunity detector       │
│  ├─ budget tracker                   │
│  └─ SQLite store (metadata + hashes) │
└─────────────────┬───────────────────┘
                  ▼
        real provider upstream
                  │
                  ▼
        UI surfaces (read-only over store + live stream):
        CLI status / local web dashboard / (later) VS Code panel
```

- **Language:** implementer’s choice; a single static binary (Go or Rust) or a Node/TS service both fit. Optimize for easy install and low overhead.
- **Config:** single file (routes, upstreams, budgets, thresholds, pricing overrides, logging opt-in).
- **No network dependency for analysis** — pricing table and tokenizers are bundled; updates are pulled on demand, not required.

-----

## 7. Milestones

1. **M1 — Transparent proxy.** Anthropic + OpenAI passthrough with streaming intact; per-request token + cost capture to SQLite. (Proves the core thesis: it can sit in front of Claude Code without breaking it.)
1. **M2 — Pre-flight estimate + budgets.** Local tokenizing, pricing table, session/daily budgets, threshold warnings.
1. **M3 — Lint engine.** The four 5.3 rules + caching detector, each with a token-waste estimate.
1. **M4 — Dashboard + receipts.** Local web view and per-response receipt.
1. **M5 — Polish.** One-command install, config docs, accuracy calibration notes for Anthropic token counts.

## 8. Success metrics

- Proxy overhead < 50 ms (non-streamed); zero streaming regressions.
- Token estimate within the documented accuracy band vs. provider-reported usage.
- A real session through Claude Code produces a correct receipt and at least one actionable lint finding on a deliberately wasteful prompt.

## 9. Open questions / risks

- **Auth passthrough:** cleanest handling of provider API keys through the proxy (env passthrough vs. proxy-held credentials). Security-sensitive — decide early.
- **Anthropic token accuracy:** approximation band acceptable for v1? Offer optional exact counting via the provider’s token-count endpoint as a paid-but-precise mode?
- **Auto-strip duplicates:** should the proxy ever mutate the request to remove exact-duplicate blocks, or strictly advise? (Default: advise only.)
- **Source labeling:** how does the proxy distinguish Claude Code vs. Cursor vs. a script for attribution — header sniffing, distinct ports/routes, or user-tagged routes?
- **Pricing freshness:** cadence and source for the bundled pricing table.

-----

*Scope note: everything in §3 is deliberately deferred. The v1 thesis is narrow on purpose — prove the proxy can transparently front real tools and deliver free, private, accurate cost visibility. The local-model right-sizing pre-pass is the intended fast-follow once M1–M4 are stable.*
