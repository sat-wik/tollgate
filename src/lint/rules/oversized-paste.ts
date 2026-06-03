import type { NormalizedRequest } from "../../adapters/types.js";
import type { Finding, LintContext, LintRule } from "./types.js";

/**
 * Flags a single contiguous text block above a token threshold — e.g. a whole
 * file pasted when only a snippet is likely needed. Estimated waste is the
 * excess over the threshold (a conservative "you probably didn't need all of
 * this" figure).
 */
export const oversizedPasteRule: LintRule = {
  id: "oversized-paste",
  run(req: NormalizedRequest, ctx: LintContext): Finding[] {
    const findings: Finding[] = [];
    const threshold = ctx.config.oversizedPasteTokens;

    req.messages.forEach((msg, messageIndex) => {
      msg.content.forEach((part, partIndex) => {
        if (part.type !== "text") return;
        const tokens = ctx.countText(part.text);
        if (tokens <= threshold) return;
        const excess = tokens - threshold;
        findings.push({
          rule: "oversized-paste",
          severity: tokens > threshold * 2 ? "high" : "warn",
          tokensWastedEst: excess,
          message: `Large pasted block (~${tokens.toLocaleString()} tokens) in message ${messageIndex}. If only a portion is needed, trimming could save up to ~${excess.toLocaleString()} tokens.`,
          location: { messageIndex, partIndex },
        });
      });
    });

    return findings;
  },
};
