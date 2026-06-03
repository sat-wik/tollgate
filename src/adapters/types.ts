// Load-bearing module contracts. See CLAUDE.md §6.1.
// Do not change these types without updating CLAUDE.md in the same PR.

export type Provider = "anthropic" | "openai";

export type NormalizedContentPart =
  | { type: "text"; text: string }
  | { type: "image"; mediaType: string; bytes: number }
  | { type: "document"; mediaType: string; bytes: number; pages?: number }
  | { type: "tool_use"; name: string; input: unknown }
  | { type: "tool_result"; toolUseId: string; content: string };

export type NormalizedMessage = {
  role: "system" | "user" | "assistant" | "tool";
  // Content normalized to an array of parts to handle multimodal uniformly.
  content: NormalizedContentPart[];
};

export type NormalizedRequest = {
  provider: Provider;
  model: string;
  messages: NormalizedMessage[];
  maxOutputTokens?: number;
  // Anything provider-specific lives here, opaque to the core.
  providerExtras: Record<string, unknown>;
  // The exact original body, used for byte-equivalent forwarding.
  rawBody: Buffer;
};

// Token usage as reported by the upstream provider (not a local estimate).
export type ActualUsage = {
  inputTokens?: number;
  outputTokens?: number;
};
