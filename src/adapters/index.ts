import type { NormalizedRequest, Provider } from "./types.js";
import { parseAnthropic } from "./anthropic.js";
import { parseOpenAI } from "./openai.js";

export function parseRequest(
  provider: Provider,
  body: unknown,
  rawBody: Buffer,
): NormalizedRequest {
  return provider === "anthropic"
    ? parseAnthropic(body, rawBody)
    : parseOpenAI(body, rawBody);
}

export * from "./types.js";
