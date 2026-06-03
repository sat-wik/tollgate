import { get_encoding, type Tiktoken } from "tiktoken";
import type { NormalizedRequest, Provider } from "../adapters/types.js";
import { OpenAITokenizer } from "./openai.js";
import { AnthropicTokenizer } from "./anthropic.js";

let textEncoder: Tiktoken | null = null;

/**
 * Deterministic token count of a bare string using o200k_base. Shared by the
 * lint engine and cache detector for token-waste estimates. Pure: same input →
 * same output (the encoder is a cached singleton, not per-call state).
 */
export function countTextTokens(text: string): number {
  textEncoder ??= get_encoding("o200k_base");
  return text ? textEncoder.encode(text).length : 0;
}

/** See CLAUDE.md §6.2. One implementation per provider family. */
export interface Tokenizer {
  countMessages(req: NormalizedRequest): { inputTokens: number; accuracy: "exact" | "approx" };
}

const openai = new OpenAITokenizer();
const anthropic = new AnthropicTokenizer();

export function getTokenizer(provider: Provider): Tokenizer {
  return provider === "openai" ? openai : anthropic;
}

/**
 * Coarse image token estimate. We only have byte counts (not dimensions), so
 * this is deliberately rough and always marks the containing estimate "approx".
 * Bounded to a high-detail-image ballpark.
 */
export function estimateImageTokens(bytes: number): number {
  if (bytes <= 0) return 85; // low-detail floor / tiny image
  return Math.min(1600, Math.max(85, Math.round(bytes / 900)));
}

/**
 * Coarse document (PDF) token estimate. Prefers a page count when known
 * (~1.5k tokens/page is a reasonable PDF ballpark); otherwise estimates from
 * bytes. Always feeds an "approx" estimate.
 */
export function estimateDocumentTokens(bytes: number, pages?: number): number {
  if (pages && pages > 0) return pages * 1500;
  if (bytes <= 0) return 500;
  return Math.min(50_000, Math.max(500, Math.round(bytes / 120)));
}

export { OpenAITokenizer, AnthropicTokenizer };
