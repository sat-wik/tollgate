import { get_encoding, type Tiktoken } from "tiktoken";
import type { NormalizedRequest } from "../adapters/types.js";
import type { Tokenizer } from "./index.js";
import { estimateImageTokens, estimateDocumentTokens } from "./index.js";

/**
 * Anthropic does not ship a public local tokenizer, so we approximate. The
 * approximation uses OpenAI's o200k_base encoding as a proxy for the text and
 * adds Anthropic's request/message framing overhead.
 *
 * Calibration (decision log 2026-06-03): measured against real Haiku 4.5
 * usage.input_tokens for single-message English prompts of 2–1643 chars, the
 * relationship is essentially `anthropic ≈ o200k(text) + 7`, i.e. a slope of ~1
 * and a fixed envelope of ~7 tokens. Modeling overhead as `REQUEST + PER_MESSAGE
 * × messages` keeps every calibration point within ±7%.
 *
 * Documented accuracy band: ±10% for English text-only requests. Multimodal and
 * heavy tool-use content widen the band (and are marked "approx", as is all
 * Anthropic output). Exact counting via the provider's token-count endpoint is a
 * possible future opt-in (PRD §9), deliberately not done here to stay offline.
 */
const REQUEST_OVERHEAD = 5;
const PER_MESSAGE_OVERHEAD = 2;

export class AnthropicTokenizer implements Tokenizer {
  private enc: Tiktoken | null = null;

  private encoder(): Tiktoken {
    return (this.enc ??= get_encoding("o200k_base"));
  }

  countMessages(req: NormalizedRequest): { inputTokens: number; accuracy: "exact" | "approx" } {
    const enc = this.encoder();
    let textTokens = 0;

    for (const msg of req.messages) {
      for (const part of msg.content) {
        switch (part.type) {
          case "text":
            textTokens += enc.encode(part.text).length;
            break;
          case "tool_result":
            textTokens += enc.encode(part.content).length;
            break;
          case "tool_use":
            textTokens += enc.encode(part.name).length + enc.encode(safeJson(part.input)).length;
            break;
          case "image":
            textTokens += estimateImageTokens(part.bytes);
            break;
          case "document":
            textTokens += estimateDocumentTokens(part.bytes, part.pages);
            break;
        }
      }
    }

    const overhead = REQUEST_OVERHEAD + PER_MESSAGE_OVERHEAD * req.messages.length;
    return { inputTokens: textTokens + overhead, accuracy: "approx" };
  }
}

function safeJson(v: unknown): string {
  if (typeof v === "string") return v;
  try {
    return JSON.stringify(v ?? "");
  } catch {
    return "";
  }
}
