import type {
  NormalizedContentPart,
  NormalizedMessage,
  NormalizedRequest,
} from "./types.js";

// Minimal shapes of the OpenAI Chat Completions request we read from.

type OpenAIPart = Record<string, unknown>;

function dataUrlBytes(url: string): number {
  // data:[<mediatype>][;base64],<data>
  const comma = url.indexOf(",");
  if (!url.startsWith("data:") || comma < 0) return 0;
  const meta = url.slice(5, comma);
  const data = url.slice(comma + 1);
  if (!meta.includes("base64")) return data.length; // URL-encoded; rough byte count
  const padding = data.endsWith("==") ? 2 : data.endsWith("=") ? 1 : 0;
  return Math.max(0, (data.length * 3) / 4 - padding);
}

function mediaTypeFromDataUrl(url: string): string {
  const semi = url.indexOf(";");
  const colon = url.indexOf(":");
  if (url.startsWith("data:") && colon >= 0) {
    const end = semi >= 0 ? semi : url.indexOf(",");
    return url.slice(colon + 1, end) || "application/octet-stream";
  }
  return "application/octet-stream";
}

function normalizePart(part: OpenAIPart): NormalizedContentPart | null {
  const type = part.type;
  if (type === "text") return { type: "text", text: String(part.text ?? "") };
  if (type === "image_url") {
    const imageUrl = part.image_url as Record<string, unknown> | undefined;
    const url = String(imageUrl?.url ?? "");
    return {
      type: "image",
      mediaType: mediaTypeFromDataUrl(url),
      bytes: dataUrlBytes(url),
    };
  }
  return null;
}

function normalizeContent(content: unknown): NormalizedContentPart[] {
  if (typeof content === "string") return [{ type: "text", text: content }];
  if (Array.isArray(content)) {
    const parts: NormalizedContentPart[] = [];
    for (const p of content) {
      if (p && typeof p === "object") {
        const np = normalizePart(p as OpenAIPart);
        if (np) parts.push(np);
      }
    }
    return parts;
  }
  return [];
}

function normalizeRole(role: unknown): NormalizedMessage["role"] {
  if (role === "system" || role === "developer") return "system";
  if (role === "assistant") return "assistant";
  if (role === "tool") return "tool";
  return "user";
}

export function parseOpenAI(body: unknown, rawBody: Buffer): NormalizedRequest {
  const b = (body ?? {}) as Record<string, unknown>;
  const messages: NormalizedMessage[] = [];

  if (Array.isArray(b.messages)) {
    for (const m of b.messages) {
      if (!m || typeof m !== "object") continue;
      const msg = m as Record<string, unknown>;
      const role = normalizeRole(msg.role);
      const content: NormalizedContentPart[] = normalizeContent(msg.content);

      // Assistant tool calls.
      if (Array.isArray(msg.tool_calls)) {
        for (const tc of msg.tool_calls) {
          const call = tc as Record<string, unknown>;
          const fn = (call.function ?? {}) as Record<string, unknown>;
          content.push({
            type: "tool_use",
            name: String(fn.name ?? ""),
            input: fn.arguments,
          });
        }
      }
      // Tool result messages carry tool_call_id.
      if (role === "tool" && typeof msg.tool_call_id === "string") {
        const text = content.find((c) => c.type === "text");
        content.length = 0;
        content.push({
          type: "tool_result",
          toolUseId: msg.tool_call_id,
          content: text && text.type === "text" ? text.text : "",
        });
      }

      messages.push({ role, content });
    }
  }

  const maxOut =
    typeof b.max_completion_tokens === "number"
      ? b.max_completion_tokens
      : typeof b.max_tokens === "number"
        ? b.max_tokens
        : undefined;

  const { model, messages: _m, ...providerExtras } = b;

  return {
    provider: "openai",
    model: String(model ?? "unknown"),
    messages,
    maxOutputTokens: maxOut,
    providerExtras,
    rawBody,
  };
}
