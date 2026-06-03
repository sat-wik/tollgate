import type { NormalizedRequest } from "../../adapters/types.js";
import type { Finding, LintContext, LintRule } from "./types.js";
import { jaccard, normalizeText, shingles } from "../../util/text.js";

type Block = {
  messageIndex: number;
  partIndex: number;
  text: string;
  normalized: string;
  tokens: number;
  shingleSet: Set<string>;
};

/**
 * Detects identical or near-identical text blocks repeated within one request
 * (e.g. the same file pasted twice, or duplicated context). Uses word-shingle
 * Jaccard similarity rather than exact string match, so reformatted repeats are
 * still caught. The later occurrence is flagged as the redundant one.
 */
export const duplicateBlockRule: LintRule = {
  id: "duplicate-block",
  run(req: NormalizedRequest, ctx: LintContext): Finding[] {
    const blocks: Block[] = [];
    req.messages.forEach((msg, messageIndex) => {
      msg.content.forEach((part, partIndex) => {
        if (part.type !== "text") return;
        const tokens = ctx.countText(part.text);
        if (tokens < ctx.config.duplicateMinTokens) return;
        const normalized = normalizeText(part.text);
        blocks.push({
          messageIndex,
          partIndex,
          text: part.text,
          normalized,
          tokens,
          shingleSet: shingles(normalized),
        });
      });
    });

    const findings: Finding[] = [];
    const flagged = new Set<number>();

    for (let j = 1; j < blocks.length; j++) {
      for (let i = 0; i < j; i++) {
        if (flagged.has(j)) break;
        const a = blocks[i];
        const b = blocks[j];
        const similar =
          a.normalized === b.normalized ||
          jaccard(a.shingleSet, b.shingleSet) >= ctx.config.duplicateSimilarity;
        if (!similar) continue;
        flagged.add(j);
        findings.push({
          rule: "duplicate-block",
          severity: "warn",
          tokensWastedEst: b.tokens,
          message: `Message ${b.messageIndex} repeats content nearly identical to message ${a.messageIndex} (~${b.tokens.toLocaleString()} tokens). Send it once and reference it instead of repeating.`,
          location: { messageIndex: b.messageIndex, partIndex: b.partIndex },
        });
      }
    }

    return findings;
  },
};
