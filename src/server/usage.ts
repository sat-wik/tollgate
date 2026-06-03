import zlib from "node:zlib";
import type { ActualUsage, Provider } from "../adapters/types.js";

/**
 * Extract provider-reported token usage from a captured response copy.
 *
 * IMPORTANT: this operates on a private copy used only for accounting. It never
 * affects the bytes streamed through to the client (CLAUDE.md §2, §7). Handles
 * both streamed (SSE) and non-streamed (JSON) responses, decompressing the copy
 * if the upstream applied content-encoding.
 */
export function extractUsage(
  provider: Provider,
  body: Buffer,
  headers: Record<string, string | string[] | undefined>,
): ActualUsage {
  const text = decode(body, headers["content-encoding"]);
  if (text == null) return {};
  const contentType = String(headers["content-type"] ?? "");
  if (contentType.includes("text/event-stream")) {
    return extractFromSSE(provider, text);
  }
  return extractFromJSON(provider, text);
}

function decode(body: Buffer, encoding: string | string[] | undefined): string | null {
  const enc = Array.isArray(encoding) ? encoding[0] : encoding;
  try {
    if (!enc || enc === "identity") return body.toString("utf8");
    if (enc === "gzip") return zlib.gunzipSync(body).toString("utf8");
    if (enc === "br") return zlib.brotliDecompressSync(body).toString("utf8");
    if (enc === "deflate") return zlib.inflateSync(body).toString("utf8");
  } catch {
    return null;
  }
  return null;
}

function readUsageObject(provider: Provider, usage: Record<string, unknown>): ActualUsage {
  if (provider === "anthropic") {
    return {
      inputTokens: numberOrUndef(usage.input_tokens),
      outputTokens: numberOrUndef(usage.output_tokens),
    };
  }
  return {
    inputTokens: numberOrUndef(usage.prompt_tokens),
    outputTokens: numberOrUndef(usage.completion_tokens),
  };
}

function extractFromJSON(provider: Provider, text: string): ActualUsage {
  try {
    const obj = JSON.parse(text) as Record<string, unknown>;
    const usage = obj.usage as Record<string, unknown> | undefined;
    return usage ? readUsageObject(provider, usage) : {};
  } catch {
    return {};
  }
}

function extractFromSSE(provider: Provider, text: string): ActualUsage {
  // Scan all `data:` payloads. Anthropic reports input on message_start and output
  // on message_delta; OpenAI reports both on the final usage chunk. We accumulate
  // across events so partial reporting is captured.
  const result: ActualUsage = {};
  for (const line of text.split(/\r?\n/)) {
    const trimmed = line.trim();
    if (!trimmed.startsWith("data:")) continue;
    const payload = trimmed.slice(5).trim();
    if (!payload || payload === "[DONE]") continue;
    let obj: Record<string, unknown>;
    try {
      obj = JSON.parse(payload) as Record<string, unknown>;
    } catch {
      continue;
    }
    const usage =
      (obj.usage as Record<string, unknown> | undefined) ??
      ((obj.message as Record<string, unknown> | undefined)?.usage as
        | Record<string, unknown>
        | undefined);
    if (usage) {
      const u = readUsageObject(provider, usage);
      if (u.inputTokens != null) result.inputTokens = u.inputTokens;
      if (u.outputTokens != null) result.outputTokens = u.outputTokens;
    }
  }
  return result;
}

function numberOrUndef(v: unknown): number | undefined {
  return typeof v === "number" ? v : undefined;
}
