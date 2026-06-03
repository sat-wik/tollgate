import { encoding_for_model, get_encoding, type Tiktoken, type TiktokenModel } from "tiktoken";
import type { NormalizedRequest } from "../adapters/types.js";
import type { Tokenizer } from "./index.js";
import { estimateImageTokens } from "./index.js";

// Chat-completion token accounting follows OpenAI's documented formula: each
// message costs a fixed framing overhead plus the encoded role and content, and
// the reply is primed with a few tokens. For text-only requests this is exact.
const TOKENS_PER_MESSAGE = 3;
const REPLY_PRIMING = 3;

export class OpenAITokenizer implements Tokenizer {
  private encoders = new Map<string, Tiktoken>();

  private encoderFor(model: string): Tiktoken {
    let enc = this.encoders.get(model);
    if (enc) return enc;
    try {
      enc = encoding_for_model(model as TiktokenModel);
    } catch {
      // Unknown/newer model id: fall back to the current base encoding.
      enc = get_encoding("o200k_base");
    }
    this.encoders.set(model, enc);
    return enc;
  }

  countMessages(req: NormalizedRequest): { inputTokens: number; accuracy: "exact" | "approx" } {
    const enc = this.encoderFor(req.model);
    let tokens = REPLY_PRIMING;
    let approx = false;

    for (const msg of req.messages) {
      tokens += TOKENS_PER_MESSAGE;
      tokens += enc.encode(msg.role).length;
      for (const part of msg.content) {
        switch (part.type) {
          case "text":
            tokens += enc.encode(part.text).length;
            break;
          case "tool_result":
            tokens += enc.encode(part.content).length;
            break;
          case "tool_use":
            // Tool-call serialization overhead varies; estimate from name + args.
            tokens += enc.encode(part.name).length + enc.encode(safeJson(part.input)).length;
            approx = true;
            break;
          case "image":
            tokens += estimateImageTokens(part.bytes);
            approx = true;
            break;
          case "document":
            tokens += estimateImageTokens(part.bytes);
            approx = true;
            break;
        }
      }
    }

    return { inputTokens: tokens, accuracy: approx ? "approx" : "exact" };
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
