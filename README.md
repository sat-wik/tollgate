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
pip install tollgate-proxy
tollgate run -- python your_app.py
```

That's the whole thing. Tollgate starts, your app runs with its AI calls going
through it, and both stop together:

```
Measuring: python your_app.py
------------------------------------------------------------------------------
15:49:34  claude-opus-5                 12,403 in      380 out     2,143ms      $0.0755
15:49:35  claude-opus-5                  8,900 in      210 out     1,004ms      $0.0498
app finished normally
------------------------------------------------------------------------------
2 call(s) this run. Totals for /Users/you/.tollgate/captured.jsonl:

model          calls  errors  input_tokens  output_tokens  cost_usd
-------------  -----  ------  ------------  -------------  --------
claude-opus-5  2      0       21,303        590            0.1253
```

**Nothing is left behind.** `run` sets the connection for that one command
only, so there is nothing to switch off afterwards and no way to end up with a
machine pointed at a proxy that isn't running. Use it when you want it; don't
when you don't.

Works with anything that starts from a terminal — `tollgate run -- npm start`,
`tollgate run -- ./my-app`, `tollgate run -- python -m myservice`.

## Leaving it on

If you'd rather measure everything without prefixing each command:

```sh
tollgate connect      # route this machine's AI traffic through Tollgate
tollgate              # leave this running in its own window
```

```sh
tollgate disconnect   # put everything back
```

`connect` writes two settings into your shell startup file between clearly
marked lines, keeps a `.tollgate-backup` copy of the file as it was, and
`disconnect` removes exactly those lines. Both tell you to open a new terminal
for the change to take effect.

The one thing to know: while connected, **Tollgate has to be running**, or your
app can't reach the AI service. If you're unsure:

```sh
tollgate status
```

```
Tollgate running on http://127.0.0.1:4141   no
Set up to capture traffic       yes  (/Users/you/.zshrc)
Active in this terminal         no
Capture file                    /Users/you/.tollgate/captured.jsonl (412 call(s))

Warning: traffic is set to go through Tollgate, but Tollgate
isn't running — apps will fail to reach the AI service.
Start it with `tollgate`, or undo with `tollgate disconnect`.
```

## Running it as a proxy directly

```sh
tollgate
```

It prints the two lines you need:

```
Tollgate listening on http://127.0.0.1:4141
Capturing to /Users/you/.tollgate/captured.jsonl

Point your app at it — no code change needed:

  export ANTHROPIC_BASE_URL=http://127.0.0.1:4141
  export OPENAI_BASE_URL=http://127.0.0.1:4141/v1

Then run your app as usual, and `tollgate report` to see the cost.
```

Export those where your app already gets its API key, start it, and watch
traffic arrive:

```
15:49:34  claude-opus-5                 12,403 in      380 out     2,143ms      $0.0755
15:49:35  gpt-4o                           800 in       95 out       412ms      $0.0030
```

Nothing else changes: your key is forwarded untouched, streaming still streams,
and unsetting the variable puts you back to talking to the provider directly.
Setting `base_url` in code works too, if you'd rather be explicit.

**You can't get the `/v1` wrong.** Anthropic's SDK appends `/v1` to a base URL
and OpenAI's expects you to supply it, so the same setting works for one and
404s the other. Tollgate accepts every shape a mis-set base URL produces —
`/messages`, `/chat/completions`, even `/v1/v1/messages` — and records them all
under the canonical endpoint, so a report never splits across them.

Then, once you have some traffic:

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

**Failures are data.** Rate limits, overloads and 5xx are captured too, with
their latency and a cost of zero. A log that silently omits the 429s is not an
audit trail. A stream the caller hangs up on is marked `truncated`, because its
usage is only a lower bound and its latency is time-to-disconnect — counted, but
kept out of the percentiles rather than dragging them down.

**Reports stream.** A busy service writes gigabytes of capture a day, so
`report` reads one record at a time and drops each body once it has been
priced: on a 104 MB log, 9.9 MB peak instead of 121.9 MB, for identical output.

**The proxy stays out of its own measurement.** Upstream connections are pooled
for the process lifetime. Opening a client per request would put a fresh TCP
and TLS handshake inside the window being timed, and a latency tool that
inflates latency is worse than no tool.

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
result against the recorded baseline — so "is Haiku cheap enough here?" and
"what did that system-prompt edit cost me?" are measurements, not estimates:

```sh
tollgate replay --live --model claude-haiku-4-5 --limit 50
tollgate replay --live --system-file prompts/v2.txt --limit 50
```

```
id                baseline_model  replay_model      cost_delta_usd  latency_delta_ms
----------------  --------------  ----------------  --------------  ----------------
9f2c1a4b…         claude-opus-5   claude-haiku-4-5  -0.0712         -2,940.00
```

Live replay costs real money, so it is careful with it. Every credential the run
needs is checked before the first request — a mixed-provider log missing one key
fails at zero cost rather than halfway through. Baselines that themselves failed
are skipped, since pairing a zero-cost 429 against a real replay reads as a cost
increase that never happened. `--limit` stops before spending on the rest.

Keys come from `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` in the environment —
Tollgate never stores the credentials from captured traffic, so it can't reuse
them. Requests are replayed non-streamed so both sides of the diff are one
complete body.

## Pricing

Rates are per million tokens, verified **2026-07-28** against both providers'
published pricing, covering the Claude 4.x/5 and GPT-4o/4.1/5.x lines plus the
o-series.

```sh
tollgate rates --model claude-opus-5
```

Three things determine what a token actually costs, and all three are modelled.

**When the call happened.** Rates are effective-dated: each model maps to a list
of rate periods, and a request is priced at the rate in force on the day it was
made. Claude Sonnet 5's introductory $2/$10 lapses on 2026-09-01 with nobody
editing a table, and a February 2026 log still prices at the long-context
premium Anthropic charged then and removed on 2026-03-13. Re-running a report is
reproducible, not merely repeatable.

```sh
tollgate rates --model claude-sonnet-5 --at 2026-08-15   # 2.00 / 10.00
tollgate rates --model claude-sonnet-5 --at 2026-09-15   # 3.00 / 15.00
```

**How the token was used.** Cache reads bill at 0.1x input. Cache writes bill at
1.25x for a five-minute TTL and 2x for an hour, split apart using Anthropic's
`cache_creation` breakdown. The cache-read discount is a property of the model,
not the provider — 90% off on the gpt-5 line, 75% on gpt-4.1 and the o-series,
50% on gpt-4o — so it is read from each model's published cached rate rather
than assumed.

**How big the request was, and on what tier.** A gpt-5.6 request over 272K input
tokens reprices *entirely* at 2x input and 1.5x output; the same was true of
Claude above 200K before March. Flex and Batch halve the bill.

Anything unknown reports `null`, never a number: an unrecognized model, or a
service tier whose multiplier isn't modelled. Priority tier is deliberately
absent — published figures disagree between 2x and 2.5x, and a guessed
multiplier is indistinguishable from a real one once it's in a report.

Batch-tier requests don't traverse this proxy (they go to the batch endpoints),
so the 50% discount applies only where a request carries `service_tier`.
Override anything with a JSON file:

```sh
TOLLGATE_PRICING=~/prices.json tollgate report
```

```json
{"gpt-6": {"input": 1.25, "output": 10.00, "cache_read_mult": 0.1}}
```

Entries merge over the built-in table, and a dated snapshot inherits its alias's
price (`claude-haiku-4-5-20251001` prices as `claude-haiku-4-5`).

## Finding caching that isn't working

Prompt caching fails silently. There is no error when a cache never hits — the
request succeeds, the response is correct, and the only symptom is a bill
several times larger than it needed to be. Tollgate holds both a fingerprint of
each prompt and the provider's own cache counters, so it can name the failure:

```sh
tollgate cache
```

```
[never_cached] claude-opus-5 — up to $2.3760 recoverable
    the same prompt ran 12 times with a 0% cache hit rate
    prompt 92987f9cc2e3d961

[invalidated] claude-sonnet-5 — up to $0.3564 recoverable
    9 distinct prompts over 9 calls wrote 198,000 tokens to cache and read
    back 0 — the prefix appears to change every call

total potentially recoverable: $2.7324
```

The second finding is the one that's hard to spot by hand: a `datetime.now()` or
a UUID somewhere in the prefix means every request writes a cache that the next
request can never read. You pay the 1.25x write premium every time and collect
the 0.1x read discount never.

Estimates are deliberately conservative — the first call in any group always has
to pay to write, so only the remainder is counted as recoverable. Prompts below
the minimum cacheable size, or seen fewer than three times, are not judged.

## Trust model

Local-first. Logs never leave your machine; requests go only to the provider
your app was already calling, authenticated with your app's own headers.
Credentials are forwarded upstream and never written to the log.

Tollgate sits in the request path of an application that would work fine
without it, so its own failures degrade observation and never the request being
observed. Capture is a tee, not a step: records go to a bounded queue drained by
a background thread, so a response never waits on a disk. With a second process
holding the capture lock, 40 concurrent requests take 172 ms against a 156 ms
baseline — writing inline made the same test 16x slower, which would have meant
inflating the very latency this tool reports. If the capture file can't be written — full disk, read-only mount,
changed permissions — the call still succeeds and Tollgate complains once on
stderr rather than turning an answer you already paid for into a 500. If the
provider can't be reached at all, the caller gets a `502` naming the transport
error and the attempt is recorded, because an unreachable provider is precisely
what a reliability log is for.

## License

MIT
