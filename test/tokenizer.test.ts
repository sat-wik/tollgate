import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { encoding_for_model, type TiktokenModel } from "tiktoken";
import { describe, expect, it } from "vitest";
import type { NormalizedMessage, NormalizedRequest } from "../src/adapters/types.js";
import { getTokenizer } from "../src/tokenizer/index.js";

const __dirname = dirname(fileURLToPath(import.meta.url));

function req(
  provider: "anthropic" | "openai",
  model: string,
  messages: NormalizedMessage[],
): NormalizedRequest {
  return { provider, model, messages, providerExtras: {}, rawBody: Buffer.from("") };
}

function userText(text: string): NormalizedMessage {
  return { role: "user", content: [{ type: "text", text }] };
}

describe("Anthropic tokenizer (M2 DoD: ±10% of real usage)", () => {
  const fixture = JSON.parse(
    readFileSync(join(__dirname, "fixtures", "anthropic-usage.json"), "utf8"),
  ) as {
    model: string;
    cases: { text: string; actualInputTokens: number }[];
    repeatedCase: { sentence: string; repeat: number; actualInputTokens: number };
  };

  const cases = [
    ...fixture.cases,
    {
      text: fixture.repeatedCase.sentence.repeat(fixture.repeatedCase.repeat),
      actualInputTokens: fixture.repeatedCase.actualInputTokens,
    },
  ];

  const tok = getTokenizer("anthropic");

  for (const c of cases) {
    const label = c.text.length > 40 ? `${c.text.slice(0, 37)}…` : c.text;
    it(`estimates "${label}" within ±10% (actual ${c.actualInputTokens})`, () => {
      const { inputTokens, accuracy } = tok.countMessages(
        req("anthropic", fixture.model, [userText(c.text)]),
      );
      const errorPct = Math.abs(inputTokens - c.actualInputTokens) / c.actualInputTokens;
      expect(accuracy).toBe("approx");
      expect(errorPct).toBeLessThanOrEqual(0.1);
    });
  }
});

describe("OpenAI tokenizer (exact for text via the documented chat formula)", () => {
  const tok = getTokenizer("openai");

  // Independent re-derivation of OpenAI's documented formula to guard the impl.
  function expectedExact(model: TiktokenModel, messages: NormalizedMessage[]): number {
    const enc = encoding_for_model(model);
    let tokens = 3; // reply priming
    for (const m of messages) {
      tokens += 3; // per-message framing
      tokens += enc.encode(m.role).length;
      for (const p of m.content) if (p.type === "text") tokens += enc.encode(p.text).length;
    }
    enc.free();
    return tokens;
  }

  it("matches the formula exactly for a single text message", () => {
    const messages = [userText("Hello, world! How are you today?")];
    const { inputTokens, accuracy } = tok.countMessages(req("openai", "gpt-4o", messages));
    expect(inputTokens).toBe(expectedExact("gpt-4o", messages));
    expect(accuracy).toBe("exact");
  });

  it("matches the formula exactly across a multi-message conversation", () => {
    const messages: NormalizedMessage[] = [
      { role: "system", content: [{ type: "text", text: "You are a concise assistant." }] },
      userText("What is the capital of France?"),
      { role: "assistant", content: [{ type: "text", text: "Paris." }] },
      userText("And of Japan?"),
    ];
    const { inputTokens } = tok.countMessages(req("openai", "gpt-4o", messages));
    expect(inputTokens).toBe(expectedExact("gpt-4o", messages));
  });

  it("falls back to a base encoding for unknown models without throwing", () => {
    const messages = [userText("some text")];
    const { inputTokens, accuracy } = tok.countMessages(
      req("openai", "gpt-9-ultra-does-not-exist", messages),
    );
    expect(inputTokens).toBeGreaterThan(0);
    expect(accuracy).toBe("exact");
  });

  it("marks multimodal requests approximate", () => {
    const messages: NormalizedMessage[] = [
      { role: "user", content: [{ type: "image", mediaType: "image/png", bytes: 50_000 }] },
    ];
    const { accuracy, inputTokens } = tok.countMessages(req("openai", "gpt-4o", messages));
    expect(accuracy).toBe("approx");
    expect(inputTokens).toBeGreaterThan(0);
  });
});
