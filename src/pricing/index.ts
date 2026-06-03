import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));

export type PriceEntry = {
  inputPerMTok: number;
  outputPerMTok: number;
  cachedInputPerMTok?: number;
};

export type PriceTable = Record<string, PriceEntry>;

// Per CLAUDE.md §6.3, the result of pricing a request. `null` from price() means
// the model is unknown — surface that in the UI, never fabricate a number.
export type PriceResult = {
  inputCost: number;
  outputCost: number;
  cachedDiscount: number;
  total: number;
  currency: "USD";
};

let bundled: PriceTable | null = null;

function loadBundled(): PriceTable {
  if (bundled) return bundled;
  const raw = JSON.parse(readFileSync(join(__dirname, "table.json"), "utf8")) as Record<
    string,
    unknown
  >;
  const table: PriceTable = {};
  for (const [key, value] of Object.entries(raw)) {
    if (key.startsWith("_")) continue; // metadata
    const v = value as Record<string, unknown>;
    if (typeof v.inputPerMTok === "number" && typeof v.outputPerMTok === "number") {
      table[key] = {
        inputPerMTok: v.inputPerMTok,
        outputPerMTok: v.outputPerMTok,
        cachedInputPerMTok:
          typeof v.cachedInputPerMTok === "number" ? v.cachedInputPerMTok : undefined,
      };
    }
  }
  bundled = table;
  return table;
}

const PER_MTOK = 1_000_000;

export class Pricing {
  private table: PriceTable;

  /** Overrides (keyed "provider/model") are merged over the bundled defaults. */
  constructor(overrides: PriceTable = {}) {
    this.table = { ...loadBundled(), ...overrides };
  }

  has(modelKey: string): boolean {
    return modelKey in this.table;
  }

  /**
   * Price a request. `cachedInput` is the count of cache-read input tokens (M3);
   * those are billed at the cached rate and the saving vs. the normal rate is
   * reported as cachedDiscount. Returns null for an unknown model.
   */
  price(
    modelKey: string,
    inputTokens: number,
    outputTokens: number,
    cachedInput = 0,
  ): PriceResult | null {
    const entry = this.table[modelKey];
    if (!entry) return null;

    const billedInput = Math.max(0, inputTokens - cachedInput);
    const inputCost = (billedInput / PER_MTOK) * entry.inputPerMTok;
    const cachedRate = entry.cachedInputPerMTok ?? entry.inputPerMTok;
    const cachedCost = (cachedInput / PER_MTOK) * cachedRate;
    const cachedDiscount = (cachedInput / PER_MTOK) * (entry.inputPerMTok - cachedRate);
    const outputCost = (outputTokens / PER_MTOK) * entry.outputPerMTok;

    return {
      inputCost: inputCost + cachedCost,
      outputCost,
      cachedDiscount,
      total: inputCost + cachedCost + outputCost,
      currency: "USD",
    };
  }
}

/** Convenience over a default (no-override) table, matching the §6.3 signature. */
export function priceRequest(
  model: string,
  input: number,
  output: number,
  cachedInput?: number,
): PriceResult | null {
  return new Pricing().price(model, input, output, cachedInput ?? 0);
}
