import { createHash } from "node:crypto";
import type { NormalizedRequest } from "../adapters/types.js";

/** Concatenate all text content of a request, in order. */
export function normalizedText(req: NormalizedRequest): string {
  const parts: string[] = [];
  for (const msg of req.messages) {
    for (const part of msg.content) {
      if (part.type === "text") parts.push(part.text);
      else if (part.type === "tool_result") parts.push(part.content);
    }
  }
  return parts.join("\n");
}

/**
 * SHA-256 of the normalized text content (CLAUDE.md §9). The hash input — raw
 * prompt text — is never logged or persisted; only this digest is.
 */
export function contentHash(req: NormalizedRequest): string {
  return createHash("sha256").update(normalizedText(req)).digest("hex");
}
