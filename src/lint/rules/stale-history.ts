import type { NormalizedMessage, NormalizedRequest } from "../../adapters/types.js";
import type { Finding, LintContext, LintRule } from "./types.js";

function messageTokens(msg: NormalizedMessage, countText: (s: string) => number): number {
  let t = 0;
  for (const part of msg.content) {
    if (part.type === "text") t += countText(part.text);
    else if (part.type === "tool_result") t += countText(part.content);
    else if (part.type === "tool_use") t += countText(part.name) + countText(safeJson(part.input));
  }
  return t;
}

/**
 * Flags conversation history beyond a configurable depth and reports the
 * cumulative token cost of the oldest turns, which are resent on every request.
 * System messages are excluded from the depth count (they're not history).
 */
export const staleHistoryRule: LintRule = {
  id: "stale-history",
  run(req: NormalizedRequest, ctx: LintContext): Finding[] {
    const depth = ctx.config.staleHistoryDepth;
    // Indices of conversational (non-system) turns, in order.
    const turns = req.messages
      .map((m, i) => ({ m, i }))
      .filter(({ m }) => m.role !== "system");

    if (turns.length <= depth) return [];

    const staleTurns = turns.slice(0, turns.length - depth);
    let staleTokens = 0;
    for (const { m } of staleTurns) staleTokens += messageTokens(m, ctx.countText);

    const firstStale = staleTurns[0];
    return [
      {
        rule: "stale-history",
        severity: "warn",
        tokensWastedEst: staleTokens,
        message: `Conversation has ${turns.length} turns; the ${staleTurns.length} oldest (~${staleTokens.toLocaleString()} tokens) are beyond the configured depth of ${depth} and are resent every request. Consider summarizing or dropping them.`,
        location: { messageIndex: firstStale.i },
      },
    ];
  },
};

function safeJson(v: unknown): string {
  if (typeof v === "string") return v;
  try {
    return JSON.stringify(v ?? "");
  } catch {
    return "";
  }
}
