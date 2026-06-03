import type { NormalizedRequest } from "../../adapters/types.js";
import type { Finding, LintContext, LintRule } from "./types.js";
import { estimateImageTokens, estimateDocumentTokens } from "../../tokenizer/index.js";

/**
 * Makes the hidden token cost of image/PDF inputs explicit ("this PDF ≈ 15k
 * tokens"). Informational by nature — images may be necessary — so estimated
 * waste is 0; the value is surfacing a cost the user can't otherwise see. Fires
 * when total multimodal tokens exceed the configured threshold.
 */
export const multimodalSurpriseRule: LintRule = {
  id: "multimodal-surprise",
  run(req: NormalizedRequest, ctx: LintContext): Finding[] {
    let total = 0;
    let count = 0;
    let firstLocation: { messageIndex: number; partIndex: number } | undefined;

    req.messages.forEach((msg, messageIndex) => {
      msg.content.forEach((part, partIndex) => {
        if (part.type === "image") total += estimateImageTokens(part.bytes);
        else if (part.type === "document") total += estimateDocumentTokens(part.bytes, part.pages);
        else return;
        count++;
        firstLocation ??= { messageIndex, partIndex };
      });
    });

    if (count === 0 || total <= ctx.config.multimodalTokens) return [];

    const noun = count === 1 ? "input" : "inputs";
    return [
      {
        rule: "multimodal-surprise",
        severity: "info",
        tokensWastedEst: 0,
        message: `${count} image/document ${noun} add roughly ${total.toLocaleString()} input tokens to this request. Estimate is approximate; verify if cost is unexpected.`,
        location: firstLocation,
      },
    ];
  },
};
