import { describe, expect, it } from "vitest";
import type { NormalizedMessage, NormalizedRequest } from "../src/adapters/types.js";
import { LintEngine } from "../src/lint/engine.js";
import { countTextTokens } from "../src/tokenizer/index.js";
import { DEFAULT_LINT_CONFIG, type LintContext } from "../src/lint/rules/types.js";
import { oversizedPasteRule } from "../src/lint/rules/oversized-paste.js";
import { duplicateBlockRule } from "../src/lint/rules/duplicate-block.js";
import { multimodalSurpriseRule } from "../src/lint/rules/multimodal-surprise.js";
import { staleHistoryRule } from "../src/lint/rules/stale-history.js";

const ctx: LintContext = { countText: countTextTokens, config: DEFAULT_LINT_CONFIG };

function req(messages: NormalizedMessage[]): NormalizedRequest {
  return { provider: "anthropic", model: "claude-test", messages, providerExtras: {}, rawBody: Buffer.from("") };
}
function user(text: string): NormalizedMessage {
  return { role: "user", content: [{ type: "text", text }] };
}
// Build a text of roughly `tokens` o200k tokens out of distinct-ish words.
function textOfTokens(tokens: number): string {
  const words: string[] = [];
  for (let i = 0; i < tokens; i++) words.push(`w${i % 97}`);
  return words.join(" ");
}

describe("oversized-paste rule", () => {
  it("POSITIVE: flags a single block above the token threshold", () => {
    const big = textOfTokens(DEFAULT_LINT_CONFIG.oversizedPasteTokens + 1500);
    const findings = oversizedPasteRule.run(req([user(big)]), ctx);
    expect(findings).toHaveLength(1);
    expect(findings[0].rule).toBe("oversized-paste");
    expect(findings[0].tokensWastedEst).toBeGreaterThan(0);
    expect(findings[0].location).toMatchObject({ messageIndex: 0, partIndex: 0 });
  });

  it("NEGATIVE: ignores a small block", () => {
    expect(oversizedPasteRule.run(req([user("just a short prompt")]), ctx)).toHaveLength(0);
  });
});

describe("duplicate-block rule", () => {
  const block = textOfTokens(120); // > duplicateMinTokens

  it("POSITIVE: flags a near-identical block repeated across messages", () => {
    const findings = duplicateBlockRule.run(req([user(block), user(`${block} .`)]), ctx);
    expect(findings).toHaveLength(1);
    expect(findings[0].rule).toBe("duplicate-block");
    expect(findings[0].location?.messageIndex).toBe(1); // the later occurrence
  });

  it("NEGATIVE: does not flag two distinct blocks", () => {
    const findings = duplicateBlockRule.run(
      req([user(textOfTokens(120)), user("a completely different and unrelated message body here")]),
      ctx,
    );
    expect(findings).toHaveLength(0);
  });

  it("NEGATIVE: ignores duplicates below the minimum size", () => {
    expect(duplicateBlockRule.run(req([user("hello there"), user("hello there")]), ctx)).toHaveLength(0);
  });
});

describe("multimodal-surprise rule", () => {
  it("POSITIVE: flags when total image/document tokens exceed the threshold", () => {
    const messages: NormalizedMessage[] = [
      {
        role: "user",
        content: [
          { type: "text", text: "Summarize this." },
          { type: "document", mediaType: "application/pdf", bytes: 0, pages: 10 }, // ~15k tokens
        ],
      },
    ];
    const findings = multimodalSurpriseRule.run(req(messages), ctx);
    expect(findings).toHaveLength(1);
    expect(findings[0].rule).toBe("multimodal-surprise");
    expect(findings[0].message).toMatch(/tokens/);
  });

  it("NEGATIVE: stays quiet for a small single image", () => {
    const messages: NormalizedMessage[] = [
      { role: "user", content: [{ type: "image", mediaType: "image/png", bytes: 50_000 }] },
    ];
    expect(multimodalSurpriseRule.run(req(messages), ctx)).toHaveLength(0);
  });
});

describe("stale-history rule", () => {
  it("POSITIVE: flags conversation turns beyond the configured depth", () => {
    const turns: NormalizedMessage[] = [];
    for (let i = 0; i < DEFAULT_LINT_CONFIG.staleHistoryDepth + 5; i++) {
      turns.push(user(`turn number ${i} with a little content to count`));
    }
    const findings = staleHistoryRule.run(req(turns), ctx);
    expect(findings).toHaveLength(1);
    expect(findings[0].rule).toBe("stale-history");
    expect(findings[0].tokensWastedEst).toBeGreaterThan(0);
  });

  it("NEGATIVE: ignores a short conversation", () => {
    expect(staleHistoryRule.run(req([user("hi"), user("hello")]), ctx)).toHaveLength(0);
  });

  it("excludes system messages from the depth count", () => {
    const messages: NormalizedMessage[] = [
      { role: "system", content: [{ type: "text", text: "system preamble" }] },
      user("one"),
      user("two"),
    ];
    expect(staleHistoryRule.run(messages.length ? req(messages) : req([]), { ...ctx, config: { ...DEFAULT_LINT_CONFIG, staleHistoryDepth: 2 } })).toHaveLength(0);
  });
});

describe("LintEngine", () => {
  it("runs all four rules and a deliberately wasteful prompt yields multiple findings", () => {
    const engine = new LintEngine();
    const dup = textOfTokens(DEFAULT_LINT_CONFIG.oversizedPasteTokens + 100);
    const findings = engine.run(
      req([
        user(dup), // oversized-paste
        user(dup), // duplicate-block (also oversized)
      ]),
    );
    const rules = new Set(findings.map((f) => f.rule));
    expect(rules.has("oversized-paste")).toBe(true);
    expect(rules.has("duplicate-block")).toBe(true);
    expect(findings.length).toBeGreaterThanOrEqual(2);
  });

  it("returns no findings for a clean short prompt", () => {
    expect(new LintEngine().run(req([user("What is 2 + 2?")]))).toHaveLength(0);
  });
});
