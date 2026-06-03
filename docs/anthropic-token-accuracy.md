# Anthropic token-count accuracy

OpenAI input tokens are counted **exactly** for text using `tiktoken` and the
documented chat-completion framing formula. Anthropic is different: there is no
official public local tokenizer, and Tollgate's design forbids spending an API
call to count tokens (the analysis must be free and offline). So the Anthropic
input-token count is a **calibrated local approximation**.

## How it works

1. Concatenate the request's text content and count it with OpenAI's
   `o200k_base` encoding (a good proxy for Anthropic's tokenizer on English).
2. Add a fixed framing overhead: **`5 + 2 × messageCount`** tokens, approximating
   Anthropic's request envelope and per-message structure.

```
estimate = o200k_base(text) + 5 + (2 × number_of_messages)
```

Multimodal (image/PDF) and heavy tool-use content widen the band and are always
marked `approx`; see "Caveats".

## Calibration data

Measured against **real `usage.input_tokens`** returned by `claude-haiku-4-5`
via the live Messages API on 2026-06-03 (single user text message, `max_tokens=1`):

| Prompt (chars) | o200k tokens | Actual (Anthropic) | Estimate | Error |
|----------------|-------------:|-------------------:|---------:|------:|
| "Hi" (2)                 | 1   | 8   | 8   | 0.0% |
| pong instruction (33)    | 7   | 15  | 14  | −6.7% |
| pangram sentence (71)    | 16  | 24  | 23  | −4.2% |
| paragraph (295)          | 56  | 63  | 63  | 0.0% |
| long passage (1643)      | 289 | 296 | 296 | 0.0% |

The relationship is essentially linear with slope ≈ 1 and a fixed envelope of
~7 tokens for a single-message request. Every calibration point lands within
**±7%**.

## Documented accuracy band

**±10%** for English, text-only requests. This is the band asserted by the test
suite (`test/tokenizer.test.ts`) against the fixtures above
(`test/fixtures/anthropic-usage.json`).

The estimate is surfaced honestly: proxied responses carry
`x-tollgate-input-accuracy: approx` for Anthropic, and the dashboard compares the
estimate against the provider's reported actual once the response completes.

## Caveats

- **Non-English / code / unusual tokens.** Synthetic or highly repetitive token
  soup (e.g. `word0 word1 …`) can diverge well beyond the band, because such
  strings tokenize very differently between `o200k_base` and Anthropic's
  tokenizer. The band is for natural text.
- **Multimodal.** Image and PDF token costs are coarse estimates from byte size
  (or page count for PDFs); they are not exact and always mark the result
  `approx`.
- **Tool use.** Tool-call schemas add Anthropic-side overhead not fully modeled
  here.

## Future: optional exact counting

Anthropic exposes a token-counting endpoint. A future opt-in "precise" mode
could call it for an exact count at the cost of one extra request — explicitly
trading the free/offline property for precision. This is deliberately **not**
enabled in v1 (PRD §9, open question).
