import type { NormalizedRequest } from "../adapters/types.js";
import { countTextTokens } from "../tokenizer/index.js";
import type { Finding, LintConfig, LintContext, LintRule } from "./rules/types.js";
import { DEFAULT_LINT_CONFIG } from "./rules/types.js";
import { duplicateBlockRule } from "./rules/duplicate-block.js";
import { oversizedPasteRule } from "./rules/oversized-paste.js";
import { multimodalSurpriseRule } from "./rules/multimodal-surprise.js";
import { staleHistoryRule } from "./rules/stale-history.js";

/** The four v1 rules (CLAUDE.md §6.4), in a stable order. */
export function defaultRules(): LintRule[] {
  return [duplicateBlockRule, oversizedPasteRule, multimodalSurpriseRule, staleHistoryRule];
}

export class LintEngine {
  private rules: LintRule[];
  private config: LintConfig;

  constructor(config: Partial<LintConfig> = {}, rules?: LintRule[]) {
    this.config = { ...DEFAULT_LINT_CONFIG, ...config };
    this.rules = rules ?? defaultRules();
  }

  run(req: NormalizedRequest): Finding[] {
    const ctx: LintContext = { countText: countTextTokens, config: this.config };
    const findings: Finding[] = [];
    for (const rule of this.rules) {
      // A buggy rule must never break a request; isolate failures.
      try {
        findings.push(...rule.run(req, ctx));
      } catch {
        // Deterministic rules shouldn't throw, but be defensive.
      }
    }
    return findings;
  }
}

export type { Finding, LintConfig } from "./rules/types.js";
