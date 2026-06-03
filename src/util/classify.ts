import type { NormalizedRequest } from "../adapters/types.js";

export type RequestType = "multimodal" | "long-context" | "short-prompt";

const LONG_CONTEXT_TOKENS = 8000;

/**
 * Coarse request-type heuristic for dashboard attribution (PRD §5.6):
 * multimodal if it carries image/document parts; long-context if the input is
 * large; otherwise short-prompt.
 */
export function classifyRequestType(req: NormalizedRequest, inputTokens: number): RequestType {
  for (const msg of req.messages) {
    for (const part of msg.content) {
      if (part.type === "image" || part.type === "document") return "multimodal";
    }
  }
  return inputTokens >= LONG_CONTEXT_TOKENS ? "long-context" : "short-prompt";
}
