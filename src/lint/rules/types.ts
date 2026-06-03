// Load-bearing lint contracts. See CLAUDE.md §6.4.
// Lint rules are PURE functions of the request: no I/O, no clock, no global state.
import type { NormalizedRequest } from "../../adapters/types.js";

export type Severity = "info" | "warn" | "high";

export type Finding = {
  rule: string; // stable kebab-case id, e.g. "duplicate-block"
  severity: Severity;
  tokensWastedEst: number; // 0 if not estimable
  message: string; // user-facing, specific, actionable
  location?: { messageIndex: number; partIndex?: number; charRange?: [number, number] };
};

export type LintConfig = {
  oversizedPasteTokens: number; // flag a single text block above this many tokens
  duplicateMinTokens: number; // ignore near-duplicate blocks smaller than this
  duplicateSimilarity: number; // Jaccard shingle similarity to treat as duplicate (0..1)
  multimodalTokens: number; // flag when total image/document tokens exceed this
  staleHistoryDepth: number; // flag conversation turns beyond this depth
};

export type LintContext = {
  // Deterministic token counter (o200k_base). Pure: same input → same output.
  countText: (text: string) => number;
  config: LintConfig;
};

export interface LintRule {
  id: string;
  run(req: NormalizedRequest, ctx: LintContext): Finding[];
}

export const DEFAULT_LINT_CONFIG: LintConfig = {
  oversizedPasteTokens: 4000,
  duplicateMinTokens: 50,
  duplicateSimilarity: 0.8,
  multimodalTokens: 5000,
  staleHistoryDepth: 20,
};
