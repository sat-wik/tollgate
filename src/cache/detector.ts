import type { NormalizedRequest } from "../adapters/types.js";
import { commonPrefixLength, normalizeText } from "../util/text.js";
import { RingWindow } from "./window.js";

export type CacheMatch = { repeatedTokens: number; matchedRequestId: string };

/** See CLAUDE.md §6.5. */
export interface CacheDetector {
  observe(req: NormalizedRequest, hash: string): void;
  detect(req: NormalizedRequest): CacheMatch | null;
}

type Entry = { id: string; prefix: string };

export type CacheConfig = {
  windowSize: number; // rolling window of recent prefixes (per detector/route)
  minPrefixTokens: number; // minimum shared prefix to surface an opportunity
};

export const DEFAULT_CACHE_CONFIG: CacheConfig = {
  windowSize: 100,
  minPrefixTokens: 1024,
};

/**
 * Computes the cacheable prefix of a request: the normalized text of the system
 * message(s) plus the first user message. This is the stable leading context
 * that prompt caching would serve from cache on repeat (CLAUDE.md §6.5: compare
 * normalized text, not raw bodies).
 */
export function cacheablePrefix(req: NormalizedRequest): string {
  const parts: string[] = [];
  for (const msg of req.messages) {
    if (msg.role === "system" || msg.role === "user") {
      const text = msg.content
        .filter((p): p is { type: "text"; text: string } => p.type === "text")
        .map((p) => p.text)
        .join("\n");
      if (text) parts.push(text);
      if (msg.role === "user") break; // stop after the first user message
    } else {
      break;
    }
  }
  return normalizeText(parts.join("\n"));
}

export class PrefixCacheDetector implements CacheDetector {
  private window: RingWindow<Entry>;

  constructor(
    private config: CacheConfig,
    private countText: (text: string) => number,
  ) {
    this.window = new RingWindow<Entry>(config.windowSize);
  }

  observe(req: NormalizedRequest, hash: string): void {
    this.window.push({ id: hash, prefix: cacheablePrefix(req) });
  }

  detect(req: NormalizedRequest): CacheMatch | null {
    const prefix = cacheablePrefix(req);
    if (!prefix) return null;

    let best: { sharedChars: number; id: string } | null = null;
    for (const entry of this.window.values()) {
      const shared = commonPrefixLength(prefix, entry.prefix);
      if (shared > 0 && (!best || shared > best.sharedChars)) {
        best = { sharedChars: shared, id: entry.id };
      }
    }
    if (!best) return null;

    const repeatedTokens = this.countText(prefix.slice(0, best.sharedChars));
    if (repeatedTokens < this.config.minPrefixTokens) return null;

    return { repeatedTokens, matchedRequestId: best.id };
  }
}
