import type { NormalizedRequest, Provider } from "../adapters/types.js";
import { OpenAITokenizer } from "./openai.js";
import { AnthropicTokenizer } from "./anthropic.js";

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
 * Coarse image token estimate used by both tokenizers. We only have byte counts
 * (not dimensions) here, so this is deliberately rough and always marks the
 * containing estimate "approx". Refined multimodal accounting lands with the
 * multimodal-surprise lint in M3.
 */
export function estimateImageTokens(bytes: number): number {
  if (bytes <= 0) return 85; // OpenAI low-detail floor / small image
  return Math.min(2000, Math.max(85, Math.round(bytes / 900)));
}

export { OpenAITokenizer, AnthropicTokenizer };
