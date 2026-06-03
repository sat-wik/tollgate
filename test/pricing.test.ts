import { describe, expect, it } from "vitest";
import { Pricing, priceRequest } from "../src/pricing/index.js";

describe("pricing", () => {
  it("prices a known model at the table rate", () => {
    const p = new Pricing();
    const r = p.price("openai/gpt-4o", 1_000_000, 1_000_000);
    expect(r).not.toBeNull();
    expect(r!.inputCost).toBeCloseTo(2.5, 6);
    expect(r!.outputCost).toBeCloseTo(10.0, 6);
    expect(r!.total).toBeCloseTo(12.5, 6);
    expect(r!.currency).toBe("USD");
  });

  it("returns null (unknown) for a model not in the table", () => {
    expect(new Pricing().price("openai/gpt-does-not-exist", 100, 100)).toBeNull();
    expect(new Pricing().has("openai/gpt-does-not-exist")).toBe(false);
  });

  it("applies the cached-input rate and reports the discount", () => {
    const p = new Pricing();
    // 1M input tokens, all cache hits, on gpt-4o (input 2.5, cached 1.25).
    const r = p.price("openai/gpt-4o", 1_000_000, 0, 1_000_000)!;
    expect(r.inputCost).toBeCloseTo(1.25, 6); // billed at cached rate
    expect(r.cachedDiscount).toBeCloseTo(1.25, 6); // saved vs. normal rate
    expect(r.outputCost).toBe(0);
  });

  it("merges per-model overrides over the bundled table", () => {
    const p = new Pricing({
      "anthropic/claude-test": { inputPerMTok: 1.0, outputPerMTok: 5.0 },
    });
    const r = p.price("anthropic/claude-test", 1_000_000, 1_000_000)!;
    expect(r.inputCost).toBeCloseTo(1.0, 6);
    expect(r.outputCost).toBeCloseTo(5.0, 6);
  });

  it("priceRequest convenience matches the default table", () => {
    const r = priceRequest("openai/gpt-4o-mini", 1_000_000, 0)!;
    expect(r.inputCost).toBeCloseTo(0.15, 6);
  });
});
