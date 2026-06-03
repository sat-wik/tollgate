# Tollgate configuration reference

Tollgate reads bundled defaults from `config/default.toml`, then merges your
overrides from `~/.tollgate/config.toml` on top. Run `tollgate init` to scaffold
that file. The config is plain [TOML](https://toml.io).

```
defaults (config/default.toml)  →  user (~/.tollgate/config.toml)  →  effective config
```

Every option below lists its default and any security note.

---

## Top level

| Key | Type | Default | Notes |
|-----|------|---------|-------|
| `port` | int | `8787` | Port the proxy **and** dashboard listen on. Bound to `127.0.0.1` only — never exposed off-host. |

## `[storage]`

| Key | Type | Default | Notes |
|-----|------|---------|-------|
| `path` | string | `~/.tollgate/tollgate.db` | SQLite file. `~` expands to your home dir. Use `:memory:` for an ephemeral store. |

**Privacy:** the store holds request **metadata + a content hash** only — never
raw prompts or responses — unless a route opts in via `raw_log` (below).

## `[routes.<name>]`

Each route exposes a provider-compatible endpoint and forwards to an upstream.
A user-provided `[routes]` table **replaces** the default set wholesale (so you
can repoint upstreams without inheriting defaults you didn't ask for).

| Key | Type | Default | Notes |
|-----|------|---------|-------|
| `provider` | `"anthropic"` \| `"openai"` | — (required) | Selects the request/response adapter. |
| `path` | string | — (required) | Local path clients call, e.g. `/v1/messages`. |
| `upstream` | string | — (required) | Real provider base URL, e.g. `https://api.anthropic.com`. |
| `label` | string | route name | Attribution label shown in the dashboard. |
| `raw_log` | bool | `false` | **Security-sensitive.** When `true`, raw request/response content for this route may be logged. Default off; only flip per-route, deliberately. |
| `inject_usage` | bool | `false` (defaults file sets `true` for OpenAI) | The single sanctioned body mutation: injects `stream_options.include_usage` on streaming OpenAI requests so token usage is reported. Set `false` to forward byte-for-byte. No effect on Anthropic. |

**Auth:** client API keys (`Authorization` / `x-api-key`) are passed straight
through to the upstream. Tollgate never stores or inspects them.

## `[budget]`

Per-session and per-day ceilings. Omit a key (or set `0`) to disable that limit.
In v1, crossing 100% produces a prominent warning but **does not block**.

| Key | Type | Default | Notes |
|-----|------|---------|-------|
| `session_tokens` | int | unset | Token ceiling for this proxy process's lifetime. |
| `session_cost` | float (USD) | unset | Cost ceiling for this session. |
| `daily_tokens` | int | unset | Token ceiling per calendar day (from the store). |
| `daily_cost` | float (USD) | unset | Cost ceiling per day. |
| `thresholds` | float[] | `[0.8, 1.0]` | Fractions at which a warning fires (once each). |
| `block` | bool | `false` | v1 keeps this off; blocking at 100% is opt-in and otherwise deferred. |

## `[pricing.overrides]`

Per-model overrides merged over the bundled table (`src/pricing/table.json`).
Keys are `"provider/model"`; values are USD per million tokens. Models absent
from both the table and overrides are priced as **unknown** (never guessed).

```toml
[pricing.overrides."anthropic/claude-haiku-4-5"]
inputPerMTok = 1.00
outputPerMTok = 5.00
cachedInputPerMTok = 0.10   # optional, for cache-read tokens
```

## `[lint]`

Deterministic context-hygiene heuristics (no model calls).

| Key | Type | Default | Notes |
|-----|------|---------|-------|
| `oversized_paste_tokens` | int | `4000` | Flag a single text block larger than this. |
| `duplicate_min_tokens` | int | `50` | Ignore near-duplicate blocks smaller than this. |
| `duplicate_similarity` | float | `0.8` | Jaccard shingle similarity to treat blocks as duplicates. |
| `multimodal_tokens` | int | `5000` | Flag when total image/document tokens exceed this. |
| `stale_history_depth` | int | `20` | Flag conversation turns beyond this depth. |

## `[cache]`

Caching-opportunity detection over a rolling window of recent request prefixes.

| Key | Type | Default | Notes |
|-----|------|---------|-------|
| `window_size` | int | `100` | Recent prefixes retained per route. |
| `min_prefix_tokens` | int | `1024` | Minimum shared prefix to surface a caching opportunity. |

---

## Example `~/.tollgate/config.toml`

```toml
port = 8787

[storage]
path = "~/.tollgate/tollgate.db"

[budget]
daily_cost = 20.00
thresholds = [0.8, 1.0]

[pricing.overrides."anthropic/claude-haiku-4-5"]
inputPerMTok = 1.00
outputPerMTok = 5.00
```

## Response headers

For programmatic consumers, every proxied response carries additive
`x-tollgate-*` headers (the response body is never altered):

| Header | Meaning |
|--------|---------|
| `x-tollgate-input-tokens-est` | Locally counted input tokens (pre-flight). |
| `x-tollgate-input-accuracy` | `exact` (OpenAI text) or `approx` (Anthropic). |
| `x-tollgate-est-input-cost-usd` | Estimated input cost, or `unknown`. |
| `x-tollgate-lint-findings` | Number of lint findings on the request. |
| `x-tollgate-tokens-wasted-est` | Summed estimated wasted tokens (when > 0). |
