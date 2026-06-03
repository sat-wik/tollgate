import type {
  NormalizedContentPart,
  NormalizedMessage,
  NormalizedRequest,
} from "./types.js";

// Minimal shapes of the Anthropic Messages API request we read from. We are
// permissive: unknown fields are preserved via providerExtras and rawBody.

type AnthropicBlock = Record<string, unknown>;

function base64Bytes(data: unknown): number {
  if (typeof data !== "string") return 0;
  // Length of decoded base64 without allocating the buffer's full string form.
  const len = data.length;
  const padding = data.endsWith("==") ? 2 : data.endsWith("=") ? 1 : 0;
  return Math.max(0, (len * 3) / 4 - padding);
}

function normalizeBlock(block: AnthropicBlock): NormalizedContentPart | null {
  const type = block.type;
  switch (type) {
    case "text":
      return { type: "text", text: String(block.text ?? "") };
    case "image": {
      const source = (block.source ?? {}) as Record<string, unknown>;
      return {
        type: "image",
        mediaType: String(source.media_type ?? "application/octet-stream"),
        bytes: base64Bytes(source.data),
      };
    }
    case "document": {
      const source = (block.source ?? {}) as Record<string, unknown>;
      return {
        type: "document",
        mediaType: String(source.media_type ?? "application/octet-stream"),
        bytes: base64Bytes(source.data),
      };
    }
    case "tool_use":
      return { type: "tool_use", name: String(block.name ?? ""), input: block.input };
    case "tool_result": {
      const content = block.content;
      const text =
        typeof content === "string"
          ? content
          : Array.isArray(content)
            ? content
                .map((c) =>
                  c && typeof c === "object" && "text" in c ? String((c as any).text) : "",
                )
                .join("\n")
            : "";
      return {
        type: "tool_result",
        toolUseId: String(block.tool_use_id ?? ""),
        content: text,
      };
    }
    default:
      return null;
  }
}

function normalizeContent(content: unknown): NormalizedContentPart[] {
  if (typeof content === "string") return [{ type: "text", text: content }];
  if (Array.isArray(content)) {
    const parts: NormalizedContentPart[] = [];
    for (const block of content) {
      if (block && typeof block === "object") {
        const p = normalizeBlock(block as AnthropicBlock);
        if (p) parts.push(p);
      }
    }
    return parts;
  }
  return [];
}

export function parseAnthropic(body: unknown, rawBody: Buffer): NormalizedRequest {
  const b = (body ?? {}) as Record<string, unknown>;
  const messages: NormalizedMessage[] = [];

  // The Anthropic system prompt is a top-level field, modeled here as a system message.
  if (b.system != null) {
    messages.push({ role: "system", content: normalizeContent(b.system) });
  }

  if (Array.isArray(b.messages)) {
    for (const m of b.messages) {
      if (!m || typeof m !== "object") continue;
      const msg = m as Record<string, unknown>;
      const role = msg.role === "assistant" ? "assistant" : "user";
      messages.push({ role, content: normalizeContent(msg.content) });
    }
  }

  const { model, messages: _m, system: _s, max_tokens: _mt, ...providerExtras } = b;

  return {
    provider: "anthropic",
    model: String(model ?? "unknown"),
    messages,
    maxOutputTokens: typeof b.max_tokens === "number" ? b.max_tokens : undefined,
    providerExtras,
    rawBody,
  };
}
