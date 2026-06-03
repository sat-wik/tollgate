import { describe, expect, it } from "vitest";
import type { NormalizedMessage, NormalizedRequest } from "../src/adapters/types.js";
import { PrefixCacheDetector, cacheablePrefix } from "../src/cache/detector.js";
import { RingWindow } from "../src/cache/window.js";
import { countTextTokens } from "../src/tokenizer/index.js";

function req(system: string, user: string): NormalizedRequest {
  const messages: NormalizedMessage[] = [
    { role: "system", content: [{ type: "text", text: system }] },
    { role: "user", content: [{ type: "text", text: user }] },
  ];
  return { provider: "anthropic", model: "claude-test", messages, providerExtras: {}, rawBody: Buffer.from("") };
}
function words(n: number): string {
  return Array.from({ length: n }, (_, i) => `tok${i % 50}`).join(" ");
}

describe("cacheablePrefix", () => {
  it("uses normalized system + first user message text", () => {
    const p = cacheablePrefix(req("SYSTEM  Prompt", "Hello   World"));
    expect(p).toBe("system prompt hello world");
  });
});

describe("PrefixCacheDetector", () => {
  const config = { windowSize: 100, minPrefixTokens: 50 };

  it("returns null when the window is empty", () => {
    const d = new PrefixCacheDetector(config, countTextTokens);
    expect(d.detect(req(words(80), "go"))).toBeNull();
  });

  it("POSITIVE: detects a long shared prefix with a recent request", () => {
    const d = new PrefixCacheDetector(config, countTextTokens);
    const system = words(120); // ~120 tokens of shared context
    d.observe(req(system, "first question"), "req-A");

    const match = d.detect(req(system, "second different question"));
    expect(match).not.toBeNull();
    expect(match!.matchedRequestId).toBe("req-A");
    expect(match!.repeatedTokens).toBeGreaterThanOrEqual(config.minPrefixTokens);
  });

  it("NEGATIVE: no match when prefixes differ", () => {
    const d = new PrefixCacheDetector(config, countTextTokens);
    d.observe(req(words(120), "q"), "req-A");
    expect(d.detect(req("an entirely unrelated short system prompt", "q"))).toBeNull();
  });

  it("NEGATIVE: a short shared prefix below the threshold does not fire", () => {
    const d = new PrefixCacheDetector(config, countTextTokens);
    d.observe(req("tiny shared", "a"), "req-A");
    expect(d.detect(req("tiny shared", "b"))).toBeNull();
  });

  it("prefers the entry with the longest shared prefix", () => {
    const d = new PrefixCacheDetector(config, countTextTokens);
    const base = words(80);
    d.observe(req(base, "x"), "short-overlap");
    d.observe(req(`${base} ${words(80)}`, "y"), "long-overlap");
    const match = d.detect(req(`${base} ${words(80)} extra`, "z"));
    expect(match!.matchedRequestId).toBe("long-overlap");
  });
});

describe("RingWindow", () => {
  it("keeps only the most recent N items, newest first", () => {
    const w = new RingWindow<number>(3);
    for (const n of [1, 2, 3, 4, 5]) w.push(n);
    expect(w.size).toBe(3);
    expect(w.values()).toEqual([5, 4, 3]);
  });
});
